"""Give every word a speaker, then group words into readable speaker turns."""
import numpy as np


def word_speakers(words, wins, labels):
    """Each word takes the speaker of the voice windows it overlaps most."""
    if not len(wins):
        return [0] * len(words)
    starts = np.array([a for a, _ in wins])
    ends = np.array([b for _, b in wins])
    out = []
    for w in words:
        a, b = w["s"], max(w["e"], w["s"] + 0.2)
        hit = (starts < b) & (ends > a)
        if not hit.any():
            out.append(None)
            continue
        votes = {}
        for lab, overlap in zip(labels[hit], np.minimum(ends[hit], b) - np.maximum(starts[hit], a)):
            votes[int(lab)] = votes.get(int(lab), 0.0) + float(overlap)
        out.append(max(votes, key=votes.get))
    # Words outside every window borrow the nearest labelled neighbour.
    for seq in (range(len(out)), reversed(range(len(out)))):
        last = None
        for i in seq:
            if out[i] is None:
                out[i] = last
            else:
                last = out[i]
    return [0 if lab is None else lab for lab in out]


def _runs(labs):
    runs, start = [], 0
    for i in range(1, len(labs) + 1):
        if i == len(labs) or labs[i] != labs[start]:
            runs.append((labs[start], start, i - 1))
            start = i
    return runs


def smooth(words, labs, seg_of_word, min_seconds=1.0, min_words=3):
    """Absorb short speaker blips inside someone else's speech, unless Whisper also heard a separate line."""
    labs = list(labs)
    seg_sizes = {}
    for s in seg_of_word:
        seg_sizes[s] = seg_sizes.get(s, 0) + 1
    for _ in range(3):
        changed = False
        runs = _runs(labs)
        for k, (lab, i0, i1) in enumerate(runs):
            if words[i1]["e"] - words[i0]["s"] >= min_seconds or i1 - i0 + 1 >= min_words:
                continue
            whole_segment = seg_of_word[i0] == seg_of_word[i1] and seg_sizes[seg_of_word[i0]] == i1 - i0 + 1
            if whole_segment:
                continue
            prev = runs[k - 1] if k > 0 else None
            nxt = runs[k + 1] if k + 1 < len(runs) else None
            if prev and nxt and prev[0] != nxt[0]:
                longer = prev if words[prev[2]]["e"] - words[prev[1]]["s"] >= words[nxt[2]]["e"] - words[nxt[1]]["s"] else nxt
                new = longer[0]
            else:
                new = (prev or nxt or (lab,))[0]
            if new != lab:
                labs[i0:i1 + 1] = [new] * (i1 - i0 + 1)
                changed = True
        if not changed:
            break
    return labs


def _is_break(words, seg_of_word, j):
    """Is there a natural pause between word j-1 and word j?"""
    return (words[j - 1]["w"].rstrip().endswith((".", "?", "!")) or seg_of_word[j] != seg_of_word[j - 1]
            or words[j]["s"] - words[j - 1]["e"] > 0.6)


def snap(words, labs, seg_of_word, reach=4):
    """Move each speaker change to the nearest sentence end or pause within a few words.

    Voice windows locate a change only to within a second or so; people rarely swap mid-sentence.
    """
    labs = list(labs)
    i = 1
    while i < len(labs):
        if labs[i] != labs[i - 1] and not _is_break(words, seg_of_word, i):
            best = next((j for d in range(1, reach + 1) for j in (i - d, i + d)
                         if 0 < j < len(labs) and _is_break(words, seg_of_word, j)), None)
            if best is not None and best < i:
                labs[best:i] = [labs[i]] * (i - best)  # the new speaker started a bit earlier
            elif best is not None:
                labs[i:best] = [labs[i - 1]] * (best - i)  # the old speaker finished their sentence
                i = best
        i += 1
    return labs


def build_turns(segments, labs, max_seconds=90.0, max_gap=2.5):
    """Split Whisper segments where the speaker changes, then join consecutive pieces of one speaker."""
    pieces, k = [], 0
    for seg in segments:
        current = None
        for w in seg["words"]:
            lab = labs[k]
            k += 1
            if current and current["speaker"] == lab:
                current["words"].append(w)
            else:
                current = {"speaker": lab, "words": [w]}
                pieces.append(current)
    turns = []
    for piece in pieces:
        last = turns[-1] if turns else None
        if (last and last["speaker"] == piece["speaker"]
                and piece["words"][0]["s"] - last["words"][-1]["e"] <= max_gap
                and piece["words"][-1]["e"] - last["words"][0]["s"] <= max_seconds):
            last["words"] += piece["words"]
        else:
            turns.append({"speaker": piece["speaker"], "words": list(piece["words"])})
    return [{"id": i + 1, "start": t["words"][0]["s"], "end": t["words"][-1]["e"], "speaker": t["speaker"],
             "text": "".join(w["w"] for w in t["words"]).strip(), "words": t["words"]}
            for i, t in enumerate(turns)]
