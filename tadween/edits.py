"""What you do to a transcript: fix text everywhere, name and merge speakers, export."""
import numpy as np

from . import config, events, speakers, store, vocab


def _turn(t, turn_id):
    for turn in t["turns"]:
        if turn["id"] == turn_id:
            return turn
    raise KeyError(turn_id)


def edit_text(tid, turn_id, text):
    """Save an edited line and work out which word fixes could apply elsewhere too."""
    with store.editing(tid) as t:
        turn = _turn(t, turn_id)
        old, turn["text"] = turn["text"], text.strip()
        if turn["text"] != old:
            turn["edited"] = True
        others = [x for x in t["turns"] if x["id"] != turn_id]
        found = vocab.suggestions(old, turn["text"])
        for s in found:
            s["count"] = len(vocab.occurrences(others, s["from"]))
    events.emit("transcript", id=tid)
    return {"turn": turn, "suggestions": found}


def occurrences(tid, phrase):
    return vocab.occurrences(store.load(tid)["turns"], phrase)


def replace_all(tid, frm, to, turn_ids=None, remember=False):
    """Swap a word or phrase in every line (or the chosen ones); optionally remember it for future calls."""
    count = 0
    with store.editing(tid) as t:
        for turn in t["turns"]:
            if turn_ids is None or turn["id"] in turn_ids:
                turn["text"], n = vocab.replace(turn["text"], frm, to)
                if n:
                    turn["edited"] = True
                    count += n
        if turn_ids is None:  # replayed if voices are regrouped later
            t["fixes"] = [f for f in t.get("fixes", []) if f["from"].lower() != frm.lower()] + [{"from": frm, "to": to}]
    if remember:
        vocab.Vocabulary().add_correction(frm, to)
    events.emit("transcript", id=tid)
    return count


def rename_speaker(tid, label, name, remember=True):
    """Name a voice. With remember, Tadween recognises it in future calls."""
    name = name.strip()
    if not name:
        raise ValueError("name is empty")
    with store.editing(tid) as t:
        s = t["speakers"][label]
        s.update(name=name, manual=True)
        if label == "ME":
            config.save_settings({"my_name": name})
        elif remember:
            cents = config.read_json(store.folder(tid) / "centroids.json", {})
            if label in cents:
                s["person"] = speakers.VoiceBank().enroll(name, np.array(cents[label], np.float32))
    events.emit("transcript", id=tid)
    return t["speakers"]


def merge_speakers(tid, src, dst):
    """Two groups are really one person: move every line of src to dst."""
    if src == dst:
        return
    with store.editing(tid) as t:
        a, b = t["speakers"][src], t["speakers"][dst]
        for turn in t["turns"]:
            if turn["speaker"] == src:
                turn["speaker"] = dst
        cents_file = store.folder(tid) / "centroids.json"
        cents = config.read_json(cents_file, {})
        if src in cents and dst in cents:
            w = np.array([a["talk"], b["talk"]], dtype=np.float32) + 1e-3
            cents[dst] = speakers.unit(np.array(cents[src]) * w[0] + np.array(cents[dst]) * w[1]).tolist()
        cents.pop(src, None)
        config.write_json(cents_file, cents)
        b["talk"] = round(a["talk"] + b["talk"], 1)
        del t["speakers"][src]
    events.emit("transcript", id=tid)


def set_turn_speaker(tid, turn_id, label):
    """Move one line to another speaker ("new" makes a fresh speaker)."""
    with store.editing(tid) as t:
        turn = _turn(t, turn_id)
        if label == "new":
            n = 1 + max([int(k[1:]) for k in t["speakers"] if k[1:].isdigit()] or [0])
            label = f"S{n}"
            t["speakers"][label] = {"label": label, "name": f"Speaker {n}", "person": None, "score": None,
                                    "manual": False, "talk": 0.0}
        old = turn["speaker"]
        turn["speaker"] = label
        dur = turn["end"] - turn["start"]
        t["speakers"][label]["talk"] = round(t["speakers"][label]["talk"] + dur, 1)
        if old in t["speakers"]:
            t["speakers"][old]["talk"] = round(max(0.0, t["speakers"][old]["talk"] - dur), 1)
            if not any(x["speaker"] == old for x in t["turns"]):
                del t["speakers"][old]
    events.emit("transcript", id=tid)


def _hms(sec, srt=False):
    ms = int(round(sec * 1000))
    h, m, s, ms = ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}" if srt else f"{h:02d}:{m:02d}:{s:02d}"


def export(t, fmt):
    """-> (filename, mime type, text)"""
    names = {k: s["name"] for k, s in t["speakers"].items()}
    who = lambda turn: names.get(turn["speaker"], turn["speaker"])  # noqa: E731
    base = t["title"]
    if fmt == "txt":
        lines = [t["title"], f"Length {_hms(t.get('duration', 0))} · Speakers: {', '.join(names.values())}", ""]
        lines += [f"[{_hms(turn['start'])}] {who(turn)}: {turn['text']}\n" for turn in t["turns"]]
        return f"{base}.txt", "text/plain", "\n".join(lines)
    if fmt == "md":
        lines = [f"# {t['title']}", "", f"*{_hms(t.get('duration', 0))} · {', '.join(names.values())}*", ""]
        lines += [f"**{who(turn)}** · `{_hms(turn['start'])}`  \n{turn['text']}\n" for turn in t["turns"]]
        return f"{base}.md", "text/markdown", "\n".join(lines)
    if fmt in ("srt", "vtt"):
        cues = []
        for i, turn in enumerate(t["turns"], 1):
            a, b = _hms(turn["start"], True), _hms(turn["end"], True)
            if fmt == "srt":
                cues.append(f"{i}\n{a} --> {b}\n{who(turn)}: {turn['text']}\n")
            else:
                cues.append(f"{a.replace(',', '.')} --> {b.replace(',', '.')}\n<v {who(turn)}>{turn['text']}\n")
        body = "\n".join(cues)
        return f"{base}.{fmt}", "text/plain" if fmt == "srt" else "text/vtt", body if fmt == "srt" else "WEBVTT\n\n" + body
    raise ValueError(f"unknown format {fmt}")
