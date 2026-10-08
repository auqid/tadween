"""Recording -> speaker-labelled transcript; also rebuilds it when voices are regrouped."""
import atexit
import difflib
import os
import queue
import shutil
import subprocess
import threading
import time
import traceback
import unicodedata
import wave
from collections import Counter
from pathlib import Path

import numpy as np

from . import align, asr, audio, config, events, speakers, speed, store, vad, vocab

live_active = threading.Event()  # set during a live call: queued jobs wait so the call stays smooth
_jobs = queue.Queue()
_data_lock = None  # held until this process exits
on_progress = None  # optional callback(stage, fraction) - the CLI prints with it


def _lock(f, wait=True):
    """Lock an open file until it's closed (or unlock with wait=None). False if another process holds it."""
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK if wait is None else msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_UN if wait is None else fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def hold_data_lock():
    """Every running Tadween, app or command line, holds a locked file in data/.running until it exits.
    True if no other one is running: then any unfinished work in the data folder was cut short."""
    global _data_lock
    running = config.DATA / ".running"
    running.mkdir(parents=True, exist_ok=True)
    _data_lock = open(running / str(os.getpid()), "w")
    _lock(_data_lock)
    atexit.register(lambda: (_data_lock.close(), Path(_data_lock.name).unlink(missing_ok=True)))
    alone = True
    for other in running.iterdir():
        if other.name == str(os.getpid()):
            continue
        try:
            with open(other, "a") as f:
                if not _lock(f, wait=False):
                    alone = False  # that Tadween is still running
                    continue
                _lock(f, wait=None)
            other.unlink()  # left by a Tadween that has gone
        except OSError:
            alone = False
    return alone


def start():
    threading.Thread(target=_work, daemon=True, name="tadween-jobs").start()
    if not hold_data_lock():  # another Tadween is running: what's unfinished may be its work in progress
        return
    for s in store.summaries():  # resume work cut short by a restart
        if s["status"] in ("queued", "processing"):
            _jobs.put(s["id"])
        elif s["status"] == "live":
            store.update(s["id"], status="error", error="The app stopped during this live session.")


def _work():
    while True:
        tid = _jobs.get()
        while live_active.is_set() or speed.running.locked():  # a speed check is timing this computer
            time.sleep(2)
        try:
            process(tid)
        except Exception:  # the queue must keep going, whatever happened to this job
            traceback.print_exc()


def import_file(path, title=None, queue_job=True):
    path = Path(path).expanduser().resolve()
    try:
        duration, _ = audio.probe(path)
    except subprocess.CalledProcessError as e:  # ffprobe says why, e.g. "No such file or directory"
        raise ValueError(f"Can't read {path.name}: {e.stderr.strip().rsplit(': ', 1)[-1]}")
    t = store.create(title or path.stem, kind="file", duration=round(duration, 2),
                     source_name=path.name, source_path=str(path))
    if queue_job:
        _jobs.put(t["id"])
    events.emit("transcripts")
    return t["id"]


def import_upload(stream, length, filename, title=None):
    t = store.create(title or Path(filename).stem, kind="file", source_name=filename)
    dst = store.folder(t["id"]) / ("upload" + Path(filename).suffix.lower()[:8])
    try:
        with open(dst, "wb") as f:
            left = length
            while left > 0:
                chunk = stream.read(min(left, 1 << 20))
                if not chunk:
                    raise ValueError("The upload was interrupted. Please try again.")
                f.write(chunk)
                left -= len(chunk)
    except Exception:  # cut short, connection lost or disk full: leave nothing half-made behind
        store.delete(t["id"])
        raise
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


class Background(threading.Thread):
    """Run fn(*args) on its own thread; result() waits for it and re-raises its error."""

    def __init__(self, fn, *args):
        super().__init__(daemon=True)
        self.fn, self.args, self.value, self.error = fn, args, None, None
        self.start()

    def run(self):
        try:
            self.value = self.fn(*self.args)
        except Exception as e:
            self.error = e

    def result(self):
        self.join()
        if self.error:
            raise self.error
        return self.value


def process(tid):
    try:
        t = store.load(tid)
    except KeyError:
        return  # deleted while it waited in the queue
    d = store.folder(tid)
    work = d / "work"
    settings = config.load_settings()
    progress = Progress(tid)
    try:
        if t.get("kind") == "live":
            _analyse_live(t, d, work, settings, progress)
        else:
            _analyse_file(t, d, work, settings, progress)
        progress("Putting names to voices", 1.0)
        with store.editing(tid) as t:  # fresh copy: the title may have changed meanwhile
            rebuild(t, settings=settings)
            _enroll_requested(t)
            t.update(status="ready", progress=None, error=None)
        config.remove(d / "audio16k.wav")  # only analysis needs it: 115 MB per hour of audio
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
    playable = None
    if src.exists() or not (wav.exists() and (d / "audio.m4a").exists()):  # a retried upload is converted already
        audio.to_wav16k(src, wav)
        playable = Background(audio.make_playable, src, d)  # the player's copy is made while Whisper works
    x = audio.read_wav(wav)
    regions = vad.speech_regions(x, progress=lambda f: progress("Finding speech", f))
    config.write_json(d / "regions.json", regions)

    voc, bank = vocab.Vocabulary(), speakers.VoiceBank()
    prompt = asr.build_prompt(settings["initial_prompt"], voc.prompt_terms() + bank.names())
    wins = speakers.make_windows(regions)
    state = {"asr": 0.0, "voices": 0.0}
    stop = threading.Event()  # Whisper failed: the voice thread quits at its next tick

    def tick(**kw):
        if stop.is_set():
            raise RuntimeError("stopped")
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
    try:
        segs = asr.transcribe(asr.Timeline(x, regions), work, prompt, settings, progress=lambda f: tick(asr=f))
    except Exception:  # left running, the voice thread would set the failed job back to 'processing'
        stop.set()
        worker.join()
        raise
    worker.join()
    if "error" in found:
        raise found["error"]
    progress("Checking for repetition loops", 1.0)
    segs, loops = asr.repair_loops(segs, x, regions, work, prompt, settings)
    if playable:
        playable.result()
    if src.parent == d:
        config.remove(src)  # a browser upload, only needed until both copies exist
    np.save(d / "embeddings.npy", found["E"])
    config.write_json(d / "windows.json", wins)
    config.write_json(d / "segments.json", segs)
    store.update(t["id"], repaired_loops=loops)


TRACKS = {"system": "call audio", "mic": "your mic"}


def words_of(text):
    """Comparable words in any script: casefolded, punctuation and symbols removed."""
    words = ("".join(c for c in w if unicodedata.category(c)[0] not in "PS") for w in text.casefold().split())
    return [w for w in words if w]


def is_echo(mine, theirs):
    """Are most of my words (a mic line) inside theirs (the call audio at that moment)?"""
    if not mine or not theirs:
        return False
    matched = sum(b.size for b in difflib.SequenceMatcher(None, mine, theirs, autojunk=False).get_matching_blocks())
    return matched / len(mine) >= 0.6


def echo_of(text, start, end, call_words):
    """Without headphones the mic also hears the call: is this mic line the call audio of that moment?"""
    mine = words_of(text)
    nearby = [w for w in call_words if start - 0.5 <= w["s"] <= end + 0.5]
    if len(mine) < 3:  # "Bye." is an echo only if the call said it at the same moment, not just before
        nearby = [w for w in nearby if abs(w["s"] - start) < 0.5]
    return is_echo(mine, words_of(" ".join(w["w"] for w in nearby)))


def _without_echo(mic_segs, call_segs):
    call_words = [w for s in call_segs for w in s["words"]]
    return [s for s in mic_segs if not echo_of(s["text"], s["start"], s["end"], call_words)]


def _live_tracks(d):
    """Each recorded track as a .wav. Stop leaves raw .pcm; so does a call cut short by a crash."""
    tracks = []
    for name in TRACKS:
        pcm, wav = d / f"{name}.pcm", d / f"{name}.wav"
        if pcm.exists():
            with open(pcm, "rb") as f, wave.open(str(wav), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(config.SAMPLE_RATE)
                while chunk := f.read(1 << 20):
                    w.writeframes(chunk)
            config.remove(pcm)
        if wav.exists():
            tracks.append(name)
    return tracks


def _analyse_live(t, d, work, settings, progress):
    """After a live call: transcribe each track again with full context (live snippets are rougher),
    then group the voices on the call audio. Your mic needs no grouping - it's you."""
    progress("Preparing audio", 0.0)
    tracks = _live_tracks(d)
    if not tracks:
        raise RuntimeError("No audio was recorded for this call.")
    playable = None
    if not (d / "audio.m4a").exists():
        playable = Background(audio.make_playable, [d / f"{name}.wav" for name in tracks], d)
    voc, bank = vocab.Vocabulary(), speakers.VoiceBank()
    prompt = asr.build_prompt(settings["initial_prompt"], voc.prompt_terms() + bank.names())
    found = {}
    for name in tracks:
        x = audio.read_wav(d / f"{name}.wav")
        found[name] = (x, vad.speech_regions(x, progress=lambda f, lab=TRACKS[name]: progress(f"Finding speech in {lab}", f)))
    if not t.get("duration"):  # a call cut short by a crash never got its length
        store.update(t["id"], duration=round(max(len(x) for x, _ in found.values()) / config.SAMPLE_RATE, 2))
    # Normally the call audio holds everyone else and the mic is you. With no speech in the call audio (people
    # in the room, a call on another device), the mic heard everyone: group its voices like a recording's.
    call_speech = bool(found.get("system", (None, []))[1])
    voice_track = "system" if call_speech else "mic" if "mic" in found else None
    wins, voices = [], None
    if voice_track:
        x, regions = found[voice_track]
        wins = speakers.make_windows(regions)
        voices = Background(speakers.embed_windows, x, wins)  # CPU work, runs while Whisper transcribes
    segs = {}
    for name, (x, regions) in found.items():
        s = asr.transcribe(asr.Timeline(x, regions), work, prompt, settings,
                           progress=lambda f, lab=TRACKS[name]: progress(f"Transcribing {lab}", f))
        segs[name], _ = asr.repair_loops(s, x, regions, work, prompt, settings)
    E = voices.result() if voices else np.zeros((0, 1), np.float32)
    if playable:
        playable.result()
    if call_speech:
        call_segs = segs.get("system", [])
        final = call_segs + [{**s, "fixed": "ME"} for s in _without_echo(segs.get("mic", []), call_segs)]
    else:
        final = segs.get("mic", [])
    np.save(d / "embeddings.npy", E)
    config.write_json(d / "windows.json", wins)
    config.write_json(d / "segments.json", final)


def rebuild(t, num_speakers=None, settings=None):
    """(Re)assign speakers and turns from the cached analysis; keeps names and replays text fixes."""
    d = store.folder(t["id"])
    settings = settings or config.load_settings()
    bank, voc = speakers.VoiceBank(), vocab.Vocabulary()
    segs = config.read_json(d / "segments.json", [])
    wins = [tuple(w) for w in config.read_json(d / "windows.json", [])]
    free = [s for s in segs if not s.get("fixed")]
    fixed = [s for s in segs if s.get("fixed")]  # e.g. your own mic in a live session
    # "How many people talk" counts you too, but only the call audio's voices are grouped.
    voices = max(1, num_speakers - len({s["fixed"] for s in fixed})) if num_speakers else None
    E = np.load(d / "embeddings.npy") if wins else None
    labels = speakers.cluster(E, wins, voices, settings["speaker_threshold"]) if wins else np.zeros(0, int)

    words = [w for s in free for w in s["words"]]
    seg_of_word = [i for i, s in enumerate(free) for _ in s["words"]]
    labs = align.word_speakers(words, wins, labels) if words else []
    labs = align.snap(words, align.smooth(words, labs, seg_of_word), seg_of_word) if words else []
    # One time-ordered pass over both tracks, so a line is only joined to the same speaker's previous one
    # when nobody else spoke in between.
    pieces, k = [], 0
    for s in free:
        pieces.append((s, labs[k:k + len(s["words"])]))
        k += len(s["words"])
    pieces += [(s, [s["fixed"]] * len(s["words"])) for s in fixed]
    pieces.sort(key=lambda p: p[0]["start"])
    turns = align.build_turns([s for s, _ in pieces], [lab for _, ls in pieces for lab in ls])

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
    who = lambda lab: named[lab]["name"].casefold()  # noqa: E731 - one person may have been split into several labels
    overlap, old_talk, new_talk, best = Counter(), Counter(), Counter(), {}
    for ot in old["turns"]:
        if ot["speaker"] in named:
            old_talk[who(ot["speaker"])] += ot["end"] - ot["start"]
    for nt in new_turns:
        new_talk[key[nt["speaker"]]] += nt["end"] - nt["start"]
        for ot in old["turns"]:
            if ot["speaker"] in named:
                ov = min(nt["end"], ot["end"]) - max(nt["start"], ot["start"])
                if ov > 0:
                    overlap[(key[nt["speaker"]], who(ot["speaker"]))] += ov
    for lab in sorted(named, key=lambda lab: -sum(t["end"] - t["start"] for t in old["turns"] if t["speaker"] == lab)):
        best.setdefault(who(lab), named[lab])  # the label with the most speech speaks for that name
    out, used = {}, set()
    for (new, person), ov in overlap.most_common():
        if new in out or person in used or new == "ME":
            continue
        used.add(person)  # a name goes to its best group or nowhere - never on to a weaker one
        if ov >= 0.5 * new_talk[new] or ov >= 0.5 * old_talk[person]:
            out[new] = {k: best[person][k] for k in ("name", "person", "manual", "remember") if k in best[person]}
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
