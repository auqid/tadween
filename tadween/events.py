"""Server-sent events: the browser hears about progress and live lines as they happen."""
import json
import queue
import threading

_subscribers = set()
_lock = threading.Lock()


def subscribe():
    q = queue.Queue(maxsize=1000)
    with _lock:
        _subscribers.add(q)
    return q


def unsubscribe(q):
    with _lock:
        _subscribers.discard(q)


def emit(kind, **data):
    message = json.dumps({"type": kind, **data}, ensure_ascii=False)
    with _lock:
        targets = list(_subscribers)
    for q in targets:
        try:
            q.put_nowait(message)
        except queue.Full:
            pass  # a stalled browser tab shouldn't block anyone else
