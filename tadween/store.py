"""Transcripts on disk: data/transcripts/<id>/transcript.json plus audio and analysis caches."""
import contextlib
import re
import shutil
import threading
from datetime import datetime

from . import config

_lock = threading.RLock()


def folder(tid):
    if not re.fullmatch(r"[a-z0-9-]+", tid or ""):
        raise KeyError(tid)
    return config.TRANSCRIPTS / tid


def _new_id(title):
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "call"
    base = f"{datetime.now():%Y%m%d-%H%M%S}-{slug}"
    tid, n = base, 2
    while folder(tid).exists():
        tid, n = f"{base}-{n}", n + 1
    return tid


def create(title, **fields):
    with _lock:
        tid = _new_id(title)
        folder(tid).mkdir(parents=True)
        t = {"id": tid, "title": title, "created": datetime.now().isoformat(timespec="seconds"),
             "status": "queued", "progress": None, "duration": 0, "speakers": {}, "turns": [], "fixes": [],
             **fields}
        save(t)
        return t


def load(tid):
    with _lock:
        t = config.read_json(folder(tid) / "transcript.json", None)
        if t is None:
            raise KeyError(tid)
        return t


def save(t):
    with _lock:
        config.write_json(folder(t["id"]) / "transcript.json", t)


def update(tid, **fields):
    with _lock:
        t = load(tid)
        t.update(fields)
        save(t)
        return t


@contextlib.contextmanager
def editing(tid):
    """Load, let the caller change it, save - all under one lock."""
    with _lock:
        t = load(tid)
        yield t
        save(t)


def summaries():
    out = []
    for path in sorted(config.TRANSCRIPTS.glob("*/transcript.json"), reverse=True):
        t = config.read_json(path, None)
        if t:
            out.append({**{k: t.get(k) for k in ("id", "title", "created", "status", "progress", "duration", "kind")},
                        "speakers": [s["name"] for s in t.get("speakers", {}).values()]})
    return out


def delete(tid):
    with _lock:
        shutil.rmtree(folder(tid), ignore_errors=True)
