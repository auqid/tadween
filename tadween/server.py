"""The local web app: JSON API, server-sent events and the static UI. Listens on 127.0.0.1 only."""
import json
import mimetypes
import queue
import re
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import config, edits, events, live, pipeline, speakers, store, vocab

ROUTES = []
TID = r"([a-z0-9-]+)"


def route(method, pattern):
    def register(fn):
        ROUTES.append((method, re.compile(f"^{pattern}$"), fn))
        return fn
    return register


class Handler(BaseHTTPRequestHandler):
    server_version = "Tadween"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _allowed(self):
        # Other websites must not drive this API: refuse foreign Host headers (DNS rebinding) and
        # require a custom header on writes, which browsers won't send cross-site without CORS.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        return self.command == "GET" or self.headers.get("X-Tadween") == "1"

    def _dispatch(self, method):
        if not self._allowed():
            return self.send_json({"error": "forbidden"}, 403)
        url = urlparse(self.path)
        self.query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        for m, rx, fn in ROUTES:
            match = rx.match(url.path)
            if m == method and match:
                try:
                    result = fn(self, *[unquote(g) for g in match.groups()])
                    if result is not None:
                        self.send_json(result)
                except KeyError as e:
                    self.send_json({"error": f"Not found: {e}"}, 404)
                except (ValueError, FileNotFoundError) as e:
                    self.send_json({"error": str(e)}, 400)
                except RuntimeError as e:
                    self.send_json({"error": str(e)}, 409)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as e:
                    traceback.print_exc()
                    self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)
                return
        if method == "GET" and not url.path.startswith("/api/"):
            return self._static(url.path)
        self.send_json({"error": "No such endpoint"}, 404)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else {}

    def send_bytes(self, data, ctype, code=200, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, code=200):
        self.send_bytes(json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", code)

    def _static(self, path):
        f = (config.WEB / ("index.html" if path in ("", "/") else path.lstrip("/"))).resolve()
        if config.WEB.resolve() not in f.parents or not f.is_file():
            return self.send_json({"error": "Not found"}, 404)
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self.send_bytes(f.read_bytes(), ctype)

    def send_file(self, path, ctype):
        """With HTTP Range support, so the audio player can seek (Safari insists on it)."""
        size = path.stat().st_size
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end:
                return self.send_bytes(b"", ctype, 416, [("Content-Range", f"bytes */{size}")])
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0 and (chunk := f.read(min(left, 1 << 16))):
                self.wfile.write(chunk)
                left -= len(chunk)


# --- app state ---------------------------------------------------------------------------------

@route("GET", "/api/state")
def state(h):
    return {"settings": config.load_settings(), "live": live.status(),
            "capture_helper": config.CAPTURE_BIN.exists(), "neural_engine": config.neural_engine()}


@route("GET", "/api/events")
def stream_events(h):
    q = events.subscribe()
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.close_connection = True
    try:
        h.wfile.write(b": connected\n\n")
        h.wfile.flush()
        while True:
            try:
                h.wfile.write(f"data: {q.get(timeout=15)}\n\n".encode())
            except queue.Empty:
                h.wfile.write(b": ping\n\n")
            h.wfile.flush()
    except OSError:
        pass
    finally:
        events.unsubscribe(q)


# --- transcripts ---------------------------------------------------------------------------------

@route("GET", "/api/transcripts")
def list_transcripts(h):
    return store.summaries()


@route("POST", "/api/transcripts/upload")
def upload(h):
    length = int(h.headers.get("Content-Length") or 0)
    if not length:
        raise ValueError("Empty upload")
    return {"id": pipeline.import_upload(h.rfile, length, h.query.get("name", "recording"), h.query.get("title"))}


@route("POST", "/api/transcripts/import")
def import_path(h):
    b = h.body()
    return {"id": pipeline.import_file(b["path"], b.get("title") or None)}


@route("GET", f"/api/transcripts/{TID}")
def get_transcript(h, tid):
    return store.load(tid)


@route("PATCH", f"/api/transcripts/{TID}")
def patch_transcript(h, tid):
    title = (h.body().get("title") or "").strip()
    if title:
        store.update(tid, title=title)
    events.emit("transcripts")
    return {"ok": True}


@route("DELETE", f"/api/transcripts/{TID}")
def delete_transcript(h, tid):
    if store.load(tid)["status"] in ("processing", "live"):
        raise RuntimeError("Wait until it has finished processing.")
    store.delete(tid)
    events.emit("transcripts")
    return {"ok": True}


@route("POST", f"/api/transcripts/{TID}/retry")
def retry(h, tid):
    t = store.load(tid)
    if t["status"] != "error":
        raise RuntimeError("Only failed transcripts can be retried.")
    store.update(tid, status="queued", error=None)
    pipeline.enqueue(tid)
    events.emit("transcripts")
    return {"ok": True}


@route("PUT", rf"/api/transcripts/{TID}/turns/(\d+)")
def edit_turn(h, tid, turn_id):
    b = h.body()
    if "speaker" in b:
        edits.set_turn_speaker(tid, int(turn_id), b["speaker"])
        return {"ok": True}
    return edits.edit_text(tid, int(turn_id), b["text"])


@route("GET", f"/api/transcripts/{TID}/occurrences")
def occurrences(h, tid):
    q = h.query.get("q", "").strip()
    return edits.occurrences(tid, q) if q else []


@route("POST", f"/api/transcripts/{TID}/replace")
def replace(h, tid):
    b = h.body()
    if not b.get("from", "").strip() or not b.get("to", "").strip():
        raise ValueError("Both the word and its replacement are needed.")
    return {"count": edits.replace_all(tid, b["from"].strip(), b["to"].strip(), b.get("turns"), b.get("remember", False))}


@route("POST", f"/api/transcripts/{TID}/speakers/([A-Z0-9]+)/rename")
def rename_speaker(h, tid, label):
    b = h.body()
    return edits.rename_speaker(tid, label, b.get("name", ""), b.get("remember", True))


@route("POST", f"/api/transcripts/{TID}/speakers/merge")
def merge_speakers(h, tid):
    b = h.body()
    edits.merge_speakers(tid, b["from"], b["into"])
    return {"ok": True}


@route("POST", f"/api/transcripts/{TID}/regroup")
def regroup(h, tid):
    n = h.body().get("speakers")
    pipeline.regroup(tid, int(n) if n else None)
    return {"ok": True}


@route("GET", f"/api/transcripts/{TID}/audio")
def transcript_audio(h, tid):
    path = store.folder(tid) / "audio.m4a"
    if not path.exists():
        raise KeyError("audio")
    h.send_file(path, "audio/mp4")


@route("GET", f"/api/transcripts/{TID}/export")
def export(h, tid):
    name, mime, text = edits.export(store.load(tid), h.query.get("format", "txt"))
    h.send_bytes(text.encode(), f"{mime}; charset=utf-8",
                 extra=[("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}")])


# --- vocabulary, people, settings ---------------------------------------------------------------

@route("GET", "/api/vocabulary")
def get_vocabulary(h):
    return vocab.Vocabulary().data


@route("POST", "/api/vocabulary/corrections")
def add_correction(h):
    b = h.body()
    if not b.get("from", "").strip() or not b.get("to", "").strip():
        raise ValueError("Both the word and its replacement are needed.")
    vocab.Vocabulary().add_correction(b["from"].strip(), b["to"].strip())
    return vocab.Vocabulary().data


@route("DELETE", "/api/vocabulary/corrections")
def remove_correction(h):
    vocab.Vocabulary().remove_correction(h.query.get("from", ""))
    return vocab.Vocabulary().data


@route("PUT", "/api/vocabulary/terms")
def set_terms(h):
    vocab.Vocabulary().set_terms(h.body().get("terms", []))
    return vocab.Vocabulary().data


@route("GET", "/api/people")
def people(h):
    return speakers.VoiceBank().summary()


@route("PATCH", "/api/people/([a-z0-9]+)")
def rename_person(h, pid):
    name = (h.body().get("name") or "").strip()
    if not name:
        raise ValueError("Name is empty")
    speakers.VoiceBank().rename(pid, name)
    return speakers.VoiceBank().summary()


@route("DELETE", "/api/people/([a-z0-9]+)")
def forget_person(h, pid):
    speakers.VoiceBank().delete(pid)
    return speakers.VoiceBank().summary()


@route("GET", "/api/settings")
def get_settings(h):
    return config.load_settings()


@route("PUT", "/api/settings")
def put_settings(h):
    return config.save_settings(h.body())


# --- live ------------------------------------------------------------------------------------------

@route("GET", "/api/live")
def live_status(h):
    return live.status()


@route("POST", "/api/live/start")
def live_start(h):
    b = h.body()
    return live.start(b.get("title") or None, b.get("mic", True), b.get("system", True), b.get("apps", ""),
                      b.get("simulate"), float(b.get("speed", 1.0)))


@route("POST", "/api/live/stop")
def live_stop(h):
    return live.stop()


@route("POST", "/api/live/speakers/([A-Z0-9]+)")
def live_rename(h, label):
    b = h.body()
    return live.rename(label, b.get("name", ""), b.get("remember", True))


def run(port=8765, open_browser=True):
    config.TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        raise SystemExit(f"Port {port} is busy - is Tadween already running? Try --port {port + 1}.")
    pipeline.start()  # only once the port is ours: a launch that can't run must not touch the running app's work
    server.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"
    print(f"Tadween is running at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.8, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        live.stop()
        server.server_close()
