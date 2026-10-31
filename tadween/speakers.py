"""Who is talking: voice embeddings, grouping voices into speakers, and remembered voices."""
import threading
import time
import uuid
from collections import Counter

import numpy as np
import sherpa_onnx
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.optimize import linear_sum_assignment

from . import config

SR = config.SAMPLE_RATE
WIN, HOP, MIN_WIN = 2.0, 1.0, 0.6  # seconds
MAX_SAMPLES = 20  # voice samples kept per remembered person
# A leftover group is a person already found in the call (one voice the grouping split in two) when it sounds
# SPLIT_MARGIN more like them than like anyone else remembered, and SAME_VOICE like their group in this call.
# On real calls split halves scored 0.62-0.81 with the person, 0.15+ above the next, and 0.72-0.85 with their
# other half; two different people in one call reached 0.62.
SPLIT_MARGIN, SAME_VOICE = 0.15, 0.7


class Embedder:
    """Speaker-embedding model (TitaNet): one shared instance, one caller at a time."""
    _instance = None
    _create = threading.Lock()

    def __init__(self):
        self.ext = sherpa_onnx.SpeakerEmbeddingExtractor(
            # Runs on the CPU while Whisper uses the GPU / Neural Engine: Settings > CPU threads, read at start.
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(config.SPEAKER_MODEL),
                                                       num_threads=config.threads()))
        self.lock = threading.Lock()

    @classmethod
    def get(cls):
        with cls._create:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __call__(self, samples):
        with self.lock:
            stream = self.ext.create_stream()
            stream.accept_waveform(SR, samples)
            stream.input_finished()
            return np.array(self.ext.compute(stream), dtype=np.float32)


def make_windows(regions):
    """Short overlapping windows over the speech; each one gets a voice embedding."""
    wins = []
    for a, b in regions:
        if b - a < WIN:
            if b - a >= MIN_WIN:
                wins.append((a, b))
            continue
        t = a
        while t + WIN <= b + 1e-6:
            wins.append((round(t, 3), round(t + WIN, 3)))
            t += HOP
        if b - wins[-1][1] > 0.3:
            wins.append((round(b - WIN, 3), round(b, 3)))
    return wins


def embed_windows(x, wins, progress=None):
    embed = Embedder.get()
    out = []
    for i, (a, b) in enumerate(wins):
        out.append(embed(x[int(a * SR):int(b * SR)]))
        if progress and i % 100 == 0:
            progress(i / max(len(wins), 1))
    if progress:
        progress(1.0)
    return np.array(out, dtype=np.float32).reshape(len(out), embed.ext.dim)  # (0, dim) when there's no speech


def unit(v):
    return v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-9)


# Embeddings are compared as they are (cosine). Subtracting the recording's average voice was tried:
# it helps a little when everyone talks equally, but when one person dominates it erases their
# identity and splits them into several "speakers", and it makes voiceprints recording-dependent.


def _viterbi(scores, spans, switch):
    """Best label path when changing speaker costs `switch` (a third of that across a pause)."""
    n, k = scores.shape
    back = np.zeros((n, k), int)
    acc = -scores[0]
    for i in range(1, n):
        penalty = switch / 3 if spans[i][0] >= spans[i - 1][1] else switch
        trans = acc[:, None] + penalty * (1 - np.eye(k))
        back[i] = trans.argmin(0)
        acc = trans.min(0) - scores[i]
    path = np.zeros(n, int)
    path[-1] = int(acc.argmin())
    for i in range(n - 1, 0, -1):
        path[i - 1] = back[i, path[i]]
    return path


def cluster(E, spans, num_speakers=None, threshold=0.8, min_seconds=None, switch=0.5):
    """Group time-ordered embeddings into speakers -> one label per row (0..k-1 by first appearance).

    A group needs `min_seconds` of speech to count as a person (default: 0.5% of all speech, 8-20 s);
    smaller groups are folded into the nearest voice.
    """
    n = len(E)
    if n < 2:
        return np.zeros(n, int)
    F = unit(E)
    Z = linkage(F, method="average", metric="cosine")
    # Talk time each span adds (overlapping windows only add up to the next window's start).
    talk_time = np.array([min(b - a, spans[i + 1][0] - a) if i + 1 < n and spans[i + 1][0] > a else b - a
                          for i, (a, b) in enumerate(spans)])
    if min_seconds is None:
        min_seconds = min(20.0, max(8.0, 0.005 * talk_time.sum()))

    def big(labels, floor=min_seconds):
        talk = Counter()
        for lab, d in zip(labels, talk_time):
            talk[lab] += d
        return [lab for lab, t in talk.most_common() if t >= floor]

    if num_speakers:
        # Tiny outlier groups don't count as speakers: cut finer until enough real ones appear. A short
        # recording may not hold min_seconds for everyone: then a third of an even share will do, and
        # failing that, take the cut that found the most.
        cuts = [fcluster(Z, k, criterion="maxclust") for k in range(num_speakers, num_speakers + 20)]
        for floor in (min_seconds, min(min_seconds, talk_time.sum() / (3 * num_speakers))):
            labels, keep = max(((c, big(c, floor)) for c in cuts), key=lambda cut: min(len(cut[1]), num_speakers))
            if len(keep) >= num_speakers:
                break
        keep = keep[:num_speakers]
    else:
        labels = fcluster(Z, threshold, criterion="distance")
        keep = big(labels)
    if not keep:
        keep = [Counter(labels).most_common(1)[0][0]]

    # Fold small groups into the nearest real speaker and settle the borders (a few k-means steps).
    cents = unit(np.stack([F[labels == lab].mean(0) for lab in keep]))
    for _ in range(3):
        assign = np.argmax(F @ cents.T, axis=1)
        cents = unit(np.stack([F[assign == j].mean(0) if (assign == j).any() else cents[j]
                               for j in range(len(keep))]))
    # Smooth over time: one odd-sounding window shouldn't flip the speaker mid-sentence.
    assign = _viterbi(F @ cents.T, spans, switch)
    order = {old: new for new, old in enumerate(dict.fromkeys(assign.tolist()))}
    return np.array([order[a] for a in assign])


def centroids(E, labels):
    """Each group's average voice: what remembered voices are compared with."""
    F = unit(E)
    return {int(lab): unit(F[labels == lab].mean(0)) for lab in np.unique(labels)}


class OnlineVoices:
    """Live labels: each new snippet joins the closest voice so far, or starts a new speaker."""

    def __init__(self, threshold=0.45):
        self.threshold = threshold
        self.groups = {}  # label -> {"sum": vector, "talk": seconds}

    def assign(self, emb, seconds, new_label):
        v = unit(emb)
        best, score = None, -1.0
        for label, g in self.groups.items():
            s = float(unit(g["sum"]) @ v)
            if s > score:
                best, score = label, s
        if best is None or (score < self.threshold and seconds >= 1.5):
            best = new_label()
            self.groups[best] = {"sum": np.zeros_like(v), "talk": 0.0}
        self.groups[best]["sum"] += v * seconds
        self.groups[best]["talk"] += seconds
        return best

    def centroid(self, label):
        return unit(self.groups[label]["sum"])


def _print(vec):
    return np.round(unit(vec), 5).tolist()


class VoiceBank:
    """Remembered voices (data/people.json): each person keeps a few voiceprints (one per naming), each with the
    speaker it came from ("transcript id/label", "" for older ones), so renaming or editing that speaker later
    corrects it instead of leaving a stranger's voice under the old name."""
    _lock = threading.Lock()

    def __init__(self):
        self.file = config.DATA / "people.json"
        self._load()

    def _load(self):
        """Changes re-read the file under the lock: another VoiceBank may have saved since this one read it."""
        self.people = config.read_json(self.file, {"people": []})["people"]
        for p in self.people:
            p["sources"] = p.get("sources") or [""] * len(p["samples"])

    def _save(self):
        config.write_json(self.file, {"people": self.people})

    def names(self):
        return [p["name"] for p in self.people]

    def summary(self):
        return [{"id": p["id"], "name": p["name"], "samples": len(p["samples"]), "updated": p["updated"]}
                for p in sorted(self.people, key=lambda p: p["name"].lower())]

    def match(self, cents, threshold, splits=False, exclude=()):
        """{label: voice} -> {label: (person_id, name, score)}: one group per person (none of `exclude`), paired so
        the scores clear the threshold by the most in total (taking the single best pair first can leave a known
        voice unnamed). With splits, a leftover group that is a person already paired, split off by the grouping
        (see SPLIT_MARGIN), is them too. Pairs come first in the result, then those."""
        people = [p for p in self.people if p["samples"] and p["id"] not in exclude]
        labels = list(cents)
        if not people or not labels:
            return {}
        groups = unit(np.stack([cents[lab] for lab in labels]).astype(np.float32))
        S = np.stack([(unit(np.array(p["samples"], np.float32)) @ groups.T).max(0) for p in people], 1)
        rows, cols = linear_sum_assignment(np.maximum(S - threshold, 0), maximize=True)
        found = {int(i): int(j) for i, j in zip(rows, cols) if S[i, j] >= threshold}
        if splits and len(people) > 1:
            second = np.sort(S, 1)[:, -2]
            owner = {j: i for i, j in found.items()}
            for i in range(len(labels)):
                j = int(S[i].argmax())
                if (i not in found and j in owner and S[i, j] >= threshold and S[i, j] - second[i] >= SPLIT_MARGIN
                        and groups[i] @ groups[owner[j]] >= SAME_VOICE):
                    found[i] = j
        return {labels[i]: (people[j]["id"], people[j]["name"], round(float(S[i, j]), 3)) for i, j in found.items()}

    def has(self, source):
        return any(source in p["sources"] for p in self.people)

    def has_from(self, tid):
        return any(src.startswith(f"{tid}/") for p in self.people for src in p["sources"])

    def _take(self, source, keep=None):
        """Remove the voiceprint saved from this speaker. Someone left with none (saved from it alone, e.g. under a
        name since corrected) is forgotten."""
        emptied = []
        for p in self.people:
            kept = [(s, src) for s, src in zip(p["samples"], p["sources"]) if src != source]
            if len(kept) < len(p["samples"]):
                p["samples"], p["sources"] = [s for s, _ in kept], [src for _, src in kept]
                if not kept and p is not keep:
                    emptied.append(p["id"])
        self.people = [p for p in self.people if p["id"] not in emptied]

    def enroll(self, name, vec, source="", replaces=()):
        """Save a voice under a name -> person id. A speaker saved again replaces the voiceprint it gave before,
        whatever name that was under; so do the speakers in `replaces` (merged into this one)."""
        with self._lock:
            self._load()
            person = next((p for p in self.people if p["name"].lower() == name.lower()), None)
            if person is None:
                person = {"id": uuid.uuid4().hex[:10], "name": name, "samples": [], "sources": [], "updated": 0}
                self.people.append(person)
            for old in {source, *replaces} - {""}:
                self._take(old, keep=person)
            person["samples"] = (person["samples"] + [_print(vec)])[-MAX_SAMPLES:]
            person["sources"] = (person["sources"] + [source])[-MAX_SAMPLES:]
            person["updated"] = time.time()
            self._save()
            return person["id"]

    def forget(self, source):
        """The voiceprint saved from this speaker is no longer theirs to give (renamed without remembering, gone)."""
        with self._lock:
            self._load()
            if self.has(source):
                self._take(source)
                self._save()

    def follow(self, source, vec):
        """Lines were moved to or from a remembered speaker: their voiceprint follows (None: no voice left)."""
        if vec is None:
            return self.forget(source)
        with self._lock:
            self._load()
            for p in self.people:
                p["samples"] = [_print(vec) if src == source else s for s, src in zip(p["samples"], p["sources"])]
            self._save()

    def regrouped(self, tid, moved, voices):
        """A transcript's voices were regrouped: a voiceprint saved from an old speaker goes with its name to the new
        one ({old: new label}) and takes its voice if it has enough ({new label: voice or None}). One whose name went
        nowhere stays, untied."""
        prefix = f"{tid}/"
        with self._lock:
            self._load()
            changed = False
            for p in self.people:
                for i, src in enumerate(p["sources"]):
                    if src.startswith(prefix):
                        new = moved.get(src[len(prefix):])
                        p["sources"][i] = prefix + new if new else ""
                        if voices.get(new) is not None:
                            p["samples"][i] = _print(voices[new])
                        changed = True
            if changed:
                self._save()

    def rename(self, pid, name):
        with self._lock:
            self._load()
            for p in self.people:
                if p["id"] == pid:
                    p["name"] = name
            self._save()

    def delete(self, pid):
        with self._lock:
            self._load()
            self.people = [p for p in self.people if p["id"] != pid]
            self._save()
