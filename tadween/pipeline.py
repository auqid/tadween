"""Recording -> speaker-labelled transcript; also rebuilds it when voices are regrouped."""
import difflib
import queue
import re
import shutil
import threading
import time
import traceback
from collections import Counter
from pathlib import Path

import numpy as np

from . import align, asr, audio, config, events, speakers, store, vad, vocab

live_active = threading.Event()  # set during a live call: queued jobs wait so the call stays smooth
_jobs = queue.Queue()
on_progress = None  # optional callback(stage, fraction) - the CLI prints with it


def start():
    threading.Thread(target=_work, daemon=True, name="tadween-jobs").start()
    for s in store.summaries():  # resume work cut short by a restart
        if s["status"] in ("queued", "processing"):
            _jobs.put(s["id"])
        elif s["status"] == "live":
            store.update(s["id"], status="error", error="The app stopped during this live session.")


def _work():
    while True:
        tid = _jobs.get()
        while live_active.is_set():
            time.sleep(2)
        process(tid)


def import_file(path, title=None, queue_job=True):
    path = Path(path).expanduser().resolve()
    duration, _ = audio.probe(path)
    t = store.create(title or path.stem, kind="file", duration=round(duration, 2),
                     source_name=path.name, source_path=str(path))
    if queue_job:
        _jobs.put(t["id"])
    events.emit("transcripts")
    return t["id"]


def import_upload(stream, length, filename, title=None):
    t = store.create(title or Path(filename).stem, kind="file", source_name=filename)
    dst = store.folder(t["id"]) / ("upload" + Path(filename).suffix.lower()[:8])
    with open(dst, "wb") as f:
        left = length
        while left > 0:
            chunk = stream.read(min(left, 1 << 20))
            if not chunk:
                break
            f.write(chunk)
            left -= len(chunk)
    try:
        duration, _ = audio.probe(dst)
    except Exception:
        store.delete(t["id"])
        raise ValueError("That file has no audio track Tadween can read.")
    store.update(t["id"], duration=round(duration, 2), source_path=str(dst))
    _jobs.put(t["id"])
    events.emit("transcripts")
    return t["id"]


def enqueue(tid):
    _jobs.put(tid)


class Progress:
    def __init__(self, tid):
        self.tid, self.last = tid, None

    def __call__(self, stage, fraction=None):
        key = (stage, None if fraction is None else int(fraction * 100))
        if key == self.last:
            return
        self.last = key
        p = {"stage": stage, "fraction": fraction}
        store.update(self.tid, status="processing", progress=p)
        events.emit("progress", id=self.tid, progress=p)
        if on_progress:
            on_progress(stage, fraction)


def process(tid):
    t = store.load(tid)
    d = store.folder(tid)
    work = d / "work"
    settings = config.load_settings()
    progress = Progress(tid)
    try:
        if t.get("kind") == "live":
            _analyse_live(d, work, settings, progress)
        else:
            _analyse_file(t, d, work, settings, progress)
        progress("Putting names to voices", 1.0)
        with store.editing(tid) as t:  # fresh copy: the title may have changed meanwhile
            rebuild(t, settings=settings)
            _enroll_requested(t)
            t.update(status="ready", progress=None, error=None)
    except Exception as e:
        traceback.print_exc()
        store.update(tid, status="error", progress=None, error=f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        events.emit("transcripts")
        events.emit("transcript", id=tid)


def _analyse_file(t, d, work, settings, progress):
    src = Path(t["source_path"])
    progress("Preparing audio", 0.0)
    wav = d / "audio16k.wav"
    audio.to_wav16k(src, wav)
    audio.make_playable(src, d)
    if src.parent == d:
        src.unlink(missing_ok=True)  # a browser upload, only needed until now
    x = audio.read_wav(wav)
    regions = vad.speech_regions(x, progress=lambda f: progress("Finding speech", f))
    config.write_json(d / "regions.json", regions)

    voc, bank = vocab.Vocabulary(), speakers.VoiceBank()
    prompt = asr.build_prompt(settings["initial_prompt"], voc.prompt_terms() + bank.names())
    wins = speakers.make_windows(regions)
    state = {"asr": 0.0, "voices": 0.0}

    def tick(**kw):
        state.update(kw)
        progress(f"Transcribing {state['asr']:.0%} · recognising voices {state['voices']:.0%}", state["asr"])

    found = {}

    def voices():  # CPU work, runs while Whisper uses the GPU
        try:
            found["E"] = speakers.embed_windows(x, wins, progress=lambda f: tick(voices=f))
        except Exception as e:
            found["error"] = e

    worker = threading.Thread(target=voices, daemon=True)
    worker.start()
    segs = asr.transcribe(asr.Timeline(x, regions), work, prompt, settings, progress=lambda f: tick(asr=f))
    worker.join()
    if "error" in found:
        raise found["error"]
    progress("Checking for repetition loops", 1.0)
    segs, loops = asr.repair_loops(segs, x, regions, work, prompt, settings)
    np.save(d / "embeddings.npy", found["E"])
    config.write_json(d / "windows.json", wins)
    config.write_json(d / "segments.json", segs)
    store.update(t["id"], repaired_loops=loops)


TRACKS = {"system": "call audio", "mic": "your mic"}


def _words(text):
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split()


def _without_echo(mic_segs, call_segs):
    """Without headphones the mic also hears the call: drop mic lines the call audio already has."""
    call_words = [w for s in call_segs for w in s["words"]]
    kept = []
    for seg in mic_segs:
        nearby = " ".join(w["w"] for w in call_words if seg["start"] - 2 <= w["s"] <= seg["end"] + 2)
        if difflib.SequenceMatcher(None, _words(seg["text"]), _words(nearby)).ratio() < 0.6:
            kept.append(seg)
    return kept


def _analyse_live(d, work, settings, progress):
    """After a live call: transcribe each track again with full context (live snippets are rougher),
    then group the voices on the call audio. Your mic needs no grouping - it's you."""
    voc, bank = vocab.Vocabulary(), speakers.VoiceBank()
    prompt = asr.build_prompt(settings["initial_prompt"], voc.prompt_terms() + bank.names())
    found = {}
    for name, label in TRACKS.items():
        if not (d / f"{name}.wav").exists():
            continue
        x = audio.read_wav(d / f"{name}.wav")
        regions = vad.speech_regions(x, progress=lambda f, lab=label: progress(f"Finding speech in {lab}", f))
        segs = asr.transcribe(asr.Timeline(x, regions), work, prompt, settings,
                              progress=lambda f, lab=label: progress(f"Transcribing {lab}", f)) if regions else []
        segs, _ = asr.repair_loops(segs, x, regions, work, prompt, settings)
        found[name] = (x, regions, segs)
    wins, E = [], np.zeros((0, 1), np.float32)
    if "system" in found:
        x, regions, _ = found["system"]
        wins = speakers.make_windows(regions)
        E = speakers.embed_windows(x, wins, progress=lambda f: progress("Recognising voices", f))
    call_segs = found.get("system", (None, None, []))[2]
    mic_segs = [{**s, "fixed": "ME"} for s in _without_echo(found.get("mic", (None, None, []))[2], call_segs)]
    np.save(d / "embeddings.npy", E)
    config.write_json(d / "windows.json", wins)
    config.write_json(d / "segments.json", call_segs + mic_segs)


def rebuild(t, num_speakers=None, settings=None):
    """(Re)assign speakers and turns from the cached analysis; keeps names and replays text fixes."""
    d = store.folder(t["id"])
    settings = settings or config.load_settings()
    bank, voc = speakers.VoiceBank(), vocab.Vocabulary()
    segs = config.read_json(d / "segments.json", [])
    wins = [tuple(w) for w in config.read_json(d / "windows.json", [])]
    E = np.load(d / "embeddings.npy") if wins else None
    labels = speakers.cluster(E, wins, num_speakers, settings["speaker_threshold"]) if wins else np.zeros(0, int)

    free = [s for s in segs if not s.get("fixed")]
    fixed = [s for s in segs if s.get("fixed")]  # e.g. your own mic in a live session
    words = [w for s in free for w in s["words"]]
    seg_of_word = [i for i, s in enumerate(free) for _ in s["words"]]
    labs = align.word_speakers(words, wins, labels) if words else []
    labs = align.snap(words, align.smooth(words, labs, seg_of_word), seg_of_word) if words else []
    turns = align.build_turns(free, labs)
    turns += align.build_turns(fixed, [w_seg["fixed"] for w_seg in fixed for _ in w_seg["words"]])
    turns.sort(key=lambda turn: turn["start"])
    for i, turn in enumerate(turns):
        turn["id"] = i + 1

    order = list(dict.fromkeys(turn["speaker"] for turn in turns))
    key, n = {}, 0
    for lab in order:
        if isinstance(lab, str):
            key[lab] = lab
        else:
            n += 1
            key[lab] = f"S{n}"
    cents = speakers.centroids(E, labels) if wins else {}
    matches = bank.match({lab: cents[lab] for lab in order if lab in cents}, settings["voice_match_threshold"])
    inherited = _inherit_names(t, turns, key)
    talk = Counter()
    for turn in turns:
        talk[turn["speaker"]] += turn["end"] - turn["start"]

    spk = {}
    for lab in order:
        k = key[lab]
        s = {"label": k, "name": settings["my_name"] if k == "ME" else f"Speaker {k[1:]}", "person": None,
             "score": None, "manual": k == "ME", "talk": round(talk[lab], 1)}
        if lab in matches:
            s["person"], s["name"], s["score"] = matches[lab]
        s.update(inherited.get(k, {}))
        spk[k] = s
    config.write_json(d / "centroids.json", {key[lab]: cents[lab].tolist() for lab in order if lab in cents})

    for turn in turns:
        turn["speaker"] = key[turn["speaker"]]
        turn["text"], fired = voc.apply(turn["text"])
        for fix in t.get("fixes", []):
            turn["text"], _ = vocab.replace(turn["text"], fix["from"], fix["to"])
        if fired:
            turn["autofix"] = fired
    t.update(speakers=spk, turns=turns, num_speakers=num_speakers)
    return t


def _inherit_names(old, new_turns, key):
    """When voices are regrouped, names the user gave carry over to the group covering the same speech."""
    named = {lab: s for lab, s in old.get("speakers", {}).items() if s.get("manual") and lab != "ME"}
    if not named or not old.get("turns"):
        return {}
    overlap = Counter()
    for nt in new_turns:
        for ot in old["turns"]:
            if ot["speaker"] in named:
                ov = min(nt["end"], ot["end"]) - max(nt["start"], ot["start"])
                if ov > 0:
                    overlap[(key[nt["speaker"]], ot["speaker"])] += ov
    out, used = {}, set()
    for (new, old_label), _ in overlap.most_common():
        if new not in out and old_label not in used and new != "ME":
            out[new] = {k: named[old_label][k] for k in ("name", "person", "manual", "remember") if k in named[old_label]}
            used.add(old_label)
    return out


def _enroll_requested(t):
    """Speakers named with "remember this voice" during a live call get enrolled once grouping is final."""
    cents = config.read_json(store.folder(t["id"]) / "centroids.json", {})
    bank = None
    for k, s in t["speakers"].items():
        if s.pop("remember", False) and k in cents:
            bank = bank or speakers.VoiceBank()
            s["person"] = bank.enroll(s["name"], np.array(cents[k], np.float32))


def regroup(tid, num_speakers=None):
    with store.editing(tid) as t:
        rebuild(t, num_speakers=num_speakers)
    events.emit("transcript", id=tid)
    return t
