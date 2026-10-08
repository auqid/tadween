"""Live transcription: your microphone and the call's audio, transcribed while people talk.

Your mic is always you. Everything else (the call) comes from system audio and is grouped by
voice as it arrives. When the call ends, the full voice analysis runs once more for final labels.
"""
import bisect
import importlib.util
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime

import numpy as np

from . import asr, audio, config, events, pipeline, speakers, store, vad, vocab
from . import speed as speed_check  # "speed" in this module is how fast a replay plays

SR = config.SAMPLE_RATE
# A computer without a GPU can fall behind, as Whisper takes as long for a short snippet as for 30 s. Then the
# snippets waiting go to Whisper together: up to PACK seconds of them, PACK_GAP seconds of silence apart, so
# each word can be traced back to the snippet it was said in.
PACK = 25.0
PACK_GAP = 1.0
_session = None
_lock = threading.Lock()


def capture_available():
    """The macOS helper is built by setup.sh; elsewhere tadween/capture.py needs the soundcard package."""
    if config.PLATFORM == "mac":
        return config.CAPTURE_BIN.exists()
    return importlib.util.find_spec("soundcard") is not None


def capture_command(source, apps=""):
    """How to record one source as 16 kHz PCM. Only the macOS helper can limit capture to some apps."""
    if config.PLATFORM == "mac":
        return [str(config.CAPTURE_BIN), source] + (["--apps", apps] if source == "system" and apps else [])
    return [sys.executable, "-m", "tadween.capture", source]


class Source(threading.Thread):
    """One audio stream (mic or call audio) cut into utterances by voice activity."""

    def __init__(self, session, name, cmd=None, simulate=None, speed=1.0):
        super().__init__(daemon=True, name=f"tadween-{name}")
        self.session, self.name, self.cmd, self.simulate, self.speed = session, name, cmd, simulate, speed
        self.vad = vad.StreamingVAD()
        self.samples = 0
        self.skipped = 0  # padding samples the VAD never saw: its times run this far behind the stream's
        self.proc = None
        self.ready = False
        self.error = None
        self.log_tail = ""
        self.speaking = False
        self.heard = False  # any speech yet? Silent call audio means the mic is hearing everyone
        self.raw = open(session.dir / f"{name}.pcm", "wb")

    def run(self):
        try:
            self._from_file() if self.simulate else self._from_helper()
        except Exception as e:
            self.error = str(e)
            events.emit("live_error", source=self.name, message=self.error)
        finally:
            for start, x in self.vad.flush():
                self.session.heard(self.name, start + self.skipped / SR, x)
            self.raw.close()

    def _from_helper(self):
        env = {**os.environ, "PYTHONPATH": str(config.ROOT)}  # so the Python helper finds the tadween package
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        threading.Thread(target=self._read_logs, daemon=True).start()
        asked = time.monotonic()
        while chunk := self.proc.stdout.read(3200):  # 0.1 s of 16 kHz s16le
            # Arrival time is capture time only if we had to wait for it: audio already waiting in the
            # pipe means this reader fell behind, not that the capture skipped anything.
            waited = time.monotonic() - asked > 0.05
            self._feed(np.frombuffer(chunk[:len(chunk) // 2 * 2], "<i2").astype(np.float32) / 32768, clock=waited)
            asked = time.monotonic()
        code = self.proc.wait()
        if code != 0 and not self.session.stopping:
            raise RuntimeError(self.log_tail or f"capture stopped (exit code {code})")

    def _read_logs(self):
        lines = []
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if line == "READY":
                self.ready = True
                events.emit("live_status", source=self.name, ready=True)
            elif line:
                lines = (lines + [line.removeprefix("tadween-capture: ")])[-3:]
                self.log_tail = "\n".join(lines)

    def _from_file(self):
        """Replays a recording as if it were the call (for trying things out without a call)."""
        x = audio.read_wav(self.simulate)
        self.ready = True
        step = SR // 10
        for i in range(0, len(x), step):
            if self.session.stopping:
                break
            self._feed(x[i:i + step], clock=False)
            time.sleep(0.1 / self.speed)

    def _feed(self, x, clock):
        if clock:  # capture can skip silence: pad so this stream stays on the session clock
            behind = (time.monotonic() - self.session.t0) - (self.samples + len(x)) / SR
            if behind > 0.5:
                n = int(behind * SR)
                self._push(np.zeros(min(n, SR), np.float32))  # enough silence to end an utterance
                if n > SR:  # Silero over minutes of zeros would hold this reader up
                    self.raw.write(bytes(2 * (n - SR)))
                    self.samples += n - SR
                    self.skipped += n - SR
        self._push(x)

    def _push(self, x):
        self.raw.write(audio.to_pcm16(x))
        self.samples += len(x)
        for start, utterance in self.vad.accept(x):
            self.heard = True
            self.session.heard(self.name, start + self.skipped / SR, utterance)
        if self.vad.speaking() != self.speaking:
            self.speaking = not self.speaking
            events.emit("live_activity", source=self.name, speaking=self.speaking, heard=self.heard)


class LiveSession:
    def __init__(self, title, mic=True, system=True, apps="", simulate=None, speed=1.0):
        if not simulate and not capture_available():
            raise RuntimeError({"mac": "The capture helper isn't built yet. Run ./setup.sh (or capture/build.sh).",
                                "windows": "Live calls need the soundcard package. Run setup.ps1 again.",
                                "linux": "Live calls need the soundcard package. Run ./setup.sh again."}[config.PLATFORM])
        if not (mic or system or simulate):
            raise ValueError("Pick at least one audio source.")
        if speed_check.running.locked():
            raise RuntimeError("A speed check is timing this computer (about a minute). Start the call when it's done.")
        self.settings = config.load_settings()
        self.t = store.create(title, kind="live", status="live")
        self.id, self.dir = self.t["id"], store.folder(self.t["id"])
        self.voc, self.bank = vocab.Vocabulary(), speakers.VoiceBank()
        self.prompt = asr.build_prompt(self.settings["initial_prompt"], self.voc.prompt_terms() + self.bank.names())
        self.voices = speakers.OnlineVoices()
        self.lines, self.speakers = [], {}
        self.queue = queue.Queue()
        self.stopping = self.running = False
        self.stop_lock = threading.Lock()
        self.next_label = 1
        self.sources = []
        if simulate:
            self.sources.append(Source(self, "system", simulate=simulate, speed=speed))
        else:
            if mic:
                self.sources.append(Source(self, "mic", capture_command("mic")))
            if system:
                self.sources.append(Source(self, "system", capture_command("system", apps)))
        if any(s.name == "mic" for s in self.sources):
            self.speakers["ME"] = {"label": "ME", "name": self.settings["my_name"], "manual": True}

    def start(self):
        pipeline.live_active.set()
        try:
            self.server = asr.WhisperServer(self.settings)
        except Exception:
            pipeline.live_active.clear()
            store.delete(self.id)
            raise
        self.t0 = time.monotonic()
        self.started = time.time()
        self.running = True
        for s in self.sources:
            s.start()
        self.worker = threading.Thread(target=self._work, daemon=True, name="tadween-live-asr")
        self.worker.start()
        events.emit("live_started", id=self.id)

    def heard(self, source, start, x):
        if len(x) >= 0.3 * SR:
            self.queue.put((source, start, x))

    def _work(self):
        while True:
            items = [self.queue.get()]
            while not self.queue.empty():  # fell behind: catch up with fewer, fuller Whisper runs
                items.append(self.queue.get_nowait())
            if any(item is None for item in items):
                return
            packs = sorted(_packs(items), key=lambda pack: pack[0][1])
            heard = []
            for i, pack in enumerate(packs):
                if self.stopping:  # the final pass transcribes the whole call again: don't make Stop wait on a backlog
                    break
                try:
                    heard += self._transcribe(pack)
                except Exception as e:
                    if not self.stopping:  # Stop closes the server under a slow snippet on purpose
                        events.emit("live_error", source=pack[0][0], message=str(e))
                # Show lines as soon as no pack still to come can hold an earlier one: the view lists them in order.
                upto = packs[i + 1][0][1] if i + 1 < len(packs) else float("inf")
                heard.sort(key=lambda lx: lx[0]["start"])
                while heard and heard[0][0]["start"] < upto:
                    self._add_safely(*heard.pop(0))
            for line, x in heard:  # stopped part way
                self._add_safely(line, x)

    def _new_label(self):
        label = f"S{self.next_label}"
        self.next_label += 1
        self.speakers[label] = {"label": label, "name": f"Speaker {label[1:]}", "manual": False}
        return label

    def _transcribe(self, pack):
        """Snippets [(source, start, x)] of one source, in one Whisper run -> [(line, x)] for those with words."""
        gap = np.zeros(int(PACK_GAP * SR), np.float32)
        joined = np.concatenate([piece for _, _, x in pack for piece in (x, gap)][:-1])
        offsets, t = [], 0.0  # where each snippet starts in the joined audio
        for _, _, x in pack:
            offsets.append(t)
            t += len(x) / SR + PACK_GAP
        context = " ".join(line["text"] for line in self.lines[-2:])[-200:]
        words = [[] for _ in pack]
        for s in self.server.transcribe(joined, f"{self.prompt} {context}".strip()):
            for w in s["words"] or [{"w": " " + s["text"], "s": s["start"], "e": s["end"], "p": 1.0}]:
                # the snippet it was said in: the gaps' midpoints are the borders
                i = max(0, bisect.bisect_right(offsets, (w["s"] + w["e"]) / 2 + PACK_GAP / 2) - 1)
                words[i].append(w)
        heard = []
        for (source, start, x), offset, ws in zip(pack, offsets, words):
            if not ws:
                continue
            length = len(x) / SR
            ws = [{**w, "s": round(start + min(max(w["s"] - offset, 0.0), length), 2),
                   "e": round(start + min(max(w["e"] - offset, 0.0), length), 2)} for w in ws]
            heard.append(({"start": round(start, 2), "end": round(start + length, 2), "source": source,
                           "text": "".join(w["w"] for w in ws).strip(), "words": ws}, x))
        return heard

    def _add_safely(self, line, x):
        try:
            self._add(line, x)
        except Exception as e:
            events.emit("live_error", source=line["source"], message=str(e))

    def _add(self, line, x):
        source = line["source"]
        if source == "mic":
            if self._echo_of_call(line):
                return
            line["speaker"] = "ME"
        else:
            self._drop_mic_echoes(line)
            seconds = len(x) / SR
            if seconds >= 0.8:
                line["speaker"] = self.voices.assign(speakers.Embedder.get()(x), seconds, self._new_label)
            else:  # too short to recognise a voice: most likely whoever spoke last on the call
                last = next((ln for ln in reversed(self.lines) if ln["source"] == "system"), None)
                line["speaker"] = last["speaker"] if last else self.voices.assign(
                    speakers.Embedder.get()(x), seconds, self._new_label)
            self._name_known_voices()
        line["text"], fired = self.voc.apply(line["text"])
        if fired:
            line["autofix"] = fired
        line["index"] = len(self.lines)
        self.lines.append(line)
        events.emit("live_line", id=self.id, line=_public(line), speakers=self.speakers)

    def _echo_of_call(self, line):
        """On speakers (not headphones) the mic also hears the call; skip what the call audio already has."""
        call_words = [w for other in self.lines[-10:] if other["source"] == "system" and not other.get("removed")
                      for w in other["words"]]
        return pipeline.echo_of(line["text"], line["start"], line["end"], call_words)

    def _drop_mic_echoes(self, line):
        for other in self.lines[-10:]:
            if (other["source"] == "mic" and not other.get("removed")
                    and pipeline.echo_of(other["text"], other["start"], other["end"], line["words"])):
                other["removed"] = True
                events.emit("live_remove", id=self.id, index=other["index"])

    def _name_known_voices(self):
        """Once a voice has enough speech, check it against people you've named before."""
        unnamed = {lab: self.voices.centroid(lab) for lab, g in self.voices.groups.items()
                   if g["talk"] >= 8 and not self.speakers[lab]["manual"] and not self.speakers[lab].get("person")}
        if not unnamed:
            return
        taken = {s.get("person") for s in self.speakers.values()}
        for lab, (pid, name, score) in self.bank.match(unnamed, self.settings["voice_match_threshold"]).items():
            if pid not in taken:
                self.speakers[lab].update(name=name, person=pid, score=score)
                events.emit("live_speakers", id=self.id, speakers=self.speakers)

    def rename(self, label, name, remember=True):
        s = self.speakers[label]
        s.update(name=name.strip(), manual=True, remember=remember and label != "ME")
        if label == "ME":
            config.save_settings({"my_name": name.strip()})
        events.emit("live_speakers", id=self.id, speakers=self.speakers)

    def status(self):
        return {"running": self.running, "id": self.id, "title": self.t["title"], "started": self.started,
                "stopping": self.stopping,
                "lines": [_public(line) for line in self.lines if not line.get("removed")],
                "speakers": self.speakers,
                "sources": [{"name": s.name, "ready": s.ready, "error": s.error, "speaking": s.speaking, "heard": s.heard}
                            for s in self.sources]}

    def stop(self):
        """End the call: save audio and lines, then queue the final voice analysis."""
        with self.stop_lock:  # a second Stop (another tab, Ctrl+C) waits for this one, then has nothing to do
            if not self.running:
                return
            self.stopping = True
            for s in self.sources:
                if s.proc and s.proc.poll() is None:
                    s.proc.terminate()
            for s in self.sources:
                s.join(timeout=15)
            self.queue.put(None)
            self.worker.join(timeout=5)  # the line being transcribed, when that's quick (a GPU or the Neural Engine)
            self.server.close()  # a slow one fails at once: the final pass transcribes everything again anyway
            self.worker.join(timeout=30)
            self.running = False
            try:
                self._save()
            finally:
                pipeline.live_active.clear()
            events.emit("live_stopped", id=self.id)
            events.emit("transcripts")

    def _save(self):
        # The raw tracks become .wav files and a playable mix in the final job, so Stop returns at once.
        length = max(((self.dir / f"{s.name}.pcm").stat().st_size // 2 for s in self.sources
                      if (self.dir / f"{s.name}.pcm").exists()), default=0)
        lines = [line for line in self.lines if not line.get("removed")]
        config.write_json(self.dir / "segments.json", [
            {"start": ln["start"], "end": ln["end"], "text": ln["text"], "words": ln["words"],
             **({"fixed": "ME"} if ln["source"] == "mic" else {})} for ln in lines])
        store.update(self.id, status="queued", duration=round(length / SR, 2), speakers=self.speakers,
                     turns=[{**_public(ln), "id": i + 1} for i, ln in enumerate(lines)])
        pipeline.enqueue(self.id)


def _packs(items):
    """Queued snippets [(source, start, x)] -> runs of one source that fit in one Whisper window."""
    packs = []
    for source in dict.fromkeys(item[0] for item in items):
        pack, length = [], 0.0
        for item in (it for it in items if it[0] == source):
            seconds = len(item[2]) / SR
            if pack and length + PACK_GAP + seconds > PACK:
                packs.append(pack)
                pack, length = [], 0.0
            length += (PACK_GAP if pack else 0.0) + seconds
            pack.append(item)
        packs.append(pack)
    return packs


def _public(line):
    return {k: line[k] for k in ("index", "start", "end", "source", "speaker", "text", "autofix") if k in line}


def start(title=None, mic=True, system=True, apps="", simulate=None, speed=1.0):
    global _session
    with _lock:
        if _session and _session.running:
            raise RuntimeError("A live session is already running.")
        now = datetime.now()  # not %-d or %-I: Windows' strftime has neither
        session = LiveSession(title or f"Call {now:%b} {now.day}, {now.hour % 12 or 12}:{now:%M %p}", mic, system, apps,
                              simulate, speed)
        session.start()
        _session = session
        return session.status()


def stop():
    with _lock:
        session = _session
    if session:
        session.stop()
        return {"id": session.id}
    return {}


def rename(label, name, remember=True):
    if not (_session and _session.running):
        raise RuntimeError("No live session is running.")
    _session.rename(label, name, remember)
    return _session.speakers


def status():
    return _session.status() if _session else {"running": False}
