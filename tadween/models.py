"""Models Tadween downloads when they're wanted, rather than in setup: the small Whisper model for live lines."""
import threading
import urllib.request

from . import config, events

SMALL_MB = 264
state = {"downloading": False, "fraction": None, "error": None}  # shown in Settings
_lock = threading.Lock()


def fetch_small(progress=None):
    """Download the small model unless it's there, reporting to Settings ("download" events) and to progress()."""
    dest = config.SMALL_MODEL
    if dest.exists():
        return
    with _lock:
        if state["downloading"]:
            raise RuntimeError("the small model is already downloading")
        state.update(downloading=True, fraction=0.0, error=None)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        with urllib.request.urlopen(config.SMALL_MODEL_URL, timeout=60) as response, open(part, "wb") as f:
            total, done = int(response.headers.get("Content-Length") or 0), 0
            while chunk := response.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total and int(done * 100 / total) != int(state["fraction"] * 100):  # whole percents
                    state["fraction"] = done / total
                    events.emit("download", **state)
                    if progress:
                        progress(done / total)
        part.replace(dest)
        state.update(downloading=False, fraction=1.0)
    except Exception as e:
        state.update(downloading=False, error=f"{type(e).__name__}: {e}")
        raise
    finally:
        events.emit("download", **state)


def fetch_small_in_background():
    """For Settings: start the download, unless it's there or under way."""
    if state["downloading"] or config.SMALL_MODEL.exists():
        return

    def work():
        try:
            fetch_small()
        except Exception:
            pass  # in state["error"], for Settings

    threading.Thread(target=work, daemon=True, name="tadween-download").start()
