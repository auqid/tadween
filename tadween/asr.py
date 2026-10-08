"""Speech to text with whisper.cpp: whole recordings via whisper-cli, live snippets via whisper-server."""
import json
import re
import socket
import subprocess
import time
import urllib.request
import uuid
import zlib
from collections import Counter
from pathlib import Path

import numpy as np

from . import audio, config

SR = config.SAMPLE_RATE
GAP = 0.3  # seconds of silence placed between packed speech regions
NON_SPEECH = re.compile(r"^\s*[\[(][^\])]*[\])]\s*$")  # [BLANK_AUDIO], (music) ...


def build_prompt(opening, terms):
    """Whisper copies its prompt's style: a punctuated sentence plus the names and terms we know."""
    terms = [t for t in dict.fromkeys(t.strip() for t in terms) if t]
    prompt = opening.strip()
    if terms:
        prompt += " Names and terms: " + ", ".join(terms) + "."
    return prompt[:600]  # Whisper keeps at most ~220 prompt tokens


class Timeline:
    """Speech regions packed back to back (silence removed), mapped back to original time.

    Whisper hallucinates in long silences, so it only ever sees speech. We do the packing
    ourselves (rather than whisper-cli --vad) so word timestamps can be mapped back too.
    """

    def __init__(self, x, regions):
        self.x = x
        self.cuts, self.spans = [], []  # cuts: sample ranges of x; spans: (packed_start, packed_end, original_start)
        t = 0.0
        for a, b in regions:
            i = min(int(a * SR), len(x))
            j = max(i, min(int(b * SR), len(x)))
            self.cuts.append((i, j))
            self.spans.append((t, t + (j - i) / SR, a))
            t += (j - i) / SR + GAP
        self._starts = np.array([s[0] for s in self.spans]) if self.spans else np.zeros(1)

    def write_wav(self, path):
        """Write the packed audio piece by piece: a packed copy of a long call would cost hundreds of MB."""
        gap = np.zeros(int(GAP * SR), np.float32)
        audio.write_wav(path, (piece for i, j in self.cuts for piece in (self.x[i:j], gap)))

    def _span(self, t):
        return self.spans[max(0, int(np.searchsorted(self._starts, t, side="right")) - 1)]

    def to_original(self, t):
        ps, pe, orig = self._span(t)
        return orig + min(max(t - ps, 0.0), pe - ps)

    def region_end(self, t):
        ps, pe, orig = self._span(t)
        return orig + (pe - ps)


def decoding_args(settings):
    """Whisper flags for the chosen speed. Measured on an M1 (8.4 min of speech): beam search 58 s on the GPU,
    33 s with the Neural Engine encoder; greedy ("fast") 26 s on the Neural Engine, 44 s on the GPU."""
    fast = settings.get("speed") == "fast"
    args = ["-bs", "1"] if fast else []
    if fast and not config.neural_engine():
        return args  # flash attention (on by default) is the GPU's big speed-up, but rules out DTW
    # DTW word timings line words up with voices more precisely; they need flash attention off.
    return args + ["-dtw", config.WHISPER_DTW_PRESET, "-nfa"]


def _run_cli(wav, out_base, prompt, settings, progress, fresh_context):
    cmd = [config.whisper_tool("whisper-cli"), "-m", str(config.WHISPER_MODEL), "-f", str(wav),
           "-l", settings["language"], "-t", str(settings["threads"]),
           "-ojf", "-of", str(out_base), "-pp", *decoding_args(settings)]
    if prompt:
        cmd += ["--prompt", prompt, "--carry-initial-prompt"]
    if fresh_context:  # no carried-over text: the cure for repetition loops
        cmd += ["-mc", "0"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace")
    tail = []
    for line in proc.stderr:
        m = re.search(r"progress =\s*(\d+)%", line)
        if m and progress:
            progress(int(m.group(1)) / 100)
        tail = (tail + [line])[-15:]
    out = Path(f"{out_base}.json")
    if proc.wait() != 0 or not out.exists():  # it exits 0 with no output when it can't read the audio
        raise RuntimeError("whisper-cli failed:\n" + "".join(tail))
    result = json.loads(out.read_text(encoding="utf-8", errors="replace"))
    config.remove(out)
    return result


def parse(result, timeline):
    """whisper-cli -ojf output -> [{start, end, text, words: [{w, s, e, p}]}] in original time."""
    segments = []
    for seg in result.get("transcription", []):
        if not seg.get("text", "").strip() or NON_SPEECH.match(seg["text"]):
            continue
        s0, s1 = seg["offsets"]["from"] / 1000, seg["offsets"]["to"] / 1000
        words = []
        for tok in seg.get("tokens", []):
            text = tok.get("text", "")
            if not text or text.strip().startswith("[_"):  # [_BEG_], [_TT_150] ...
                continue
            t = tok["t_dtw"] / 100 if tok.get("t_dtw", -1) >= 0 else tok["offsets"]["from"] / 1000
            if words and not text.startswith(" "):  # sub-word piece or punctuation
                words[-1]["w"] += text
                words[-1]["p"] = min(words[-1]["p"], tok.get("p", 1.0))
            else:
                words.append({"w": text, "t": t, "p": tok.get("p", 1.0)})
        words = [w for w in words if w["w"].strip() not in ("-", "–", "—")]  # subtitle-style "- " turn dashes
        if not words:
            continue
        prev = s0
        for w in words:  # DTW times can wobble; keep them ordered and inside the segment
            w["t"] = prev = min(max(w["t"], prev), s1)
        out = []
        for i, w in enumerate(words):
            start = timeline.to_original(w["t"])
            nxt = words[i + 1]["t"] if i + 1 < len(words) else s1
            end = min(timeline.to_original(nxt), timeline.region_end(w["t"]))
            out.append({"w": w["w"], "s": round(start, 2), "e": round(max(end, start + 0.05), 2),
                        "p": round(float(w["p"]), 3)})
        segments.append({"start": out[0]["s"], "end": out[-1]["e"],
                         "text": "".join(w["w"] for w in out).strip(), "words": out})
    return segments


def transcribe(timeline, workdir, prompt, settings, progress=None, fresh_context=False):
    if not timeline.spans:  # no speech found: nothing for Whisper (and it can't read an empty file)
        return []
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    name = uuid.uuid4().hex[:8]
    wav = workdir / f"{name}.wav"
    timeline.write_wav(wav)
    try:
        result = _run_cli(wav, workdir / name, prompt, settings, progress, fresh_context)
    finally:
        config.remove(wav)
    return parse(result, timeline)


def _norm(text):
    return re.sub(r"[^a-z0-9' ]+", "", text.lower()).split()


def _too_repetitive(text):
    """Whisper's own test: text that compresses too well is a loop, not speech."""
    data = text.lower().encode()
    return len(data) >= 120 and len(data) / len(zlib.compress(data)) > 2.4


def find_loops(segments):
    """[(i, j)] index ranges that look like a Whisper repetition loop."""
    flagged = set()
    for i, seg in enumerate(segments):
        words = _norm(seg["text"])
        grams = Counter(tuple(words[k:k + 4]) for k in range(len(words) - 3))
        if grams and grams.most_common(1)[0][1] >= 4:
            flagged.add(i)
        if len(words) >= 3:
            same = [i + k for k, s in enumerate(segments[i:i + 6]) if _norm(s["text"]) == words]
            if len(same) >= 3:
                flagged.update(range(i, same[-1] + 1))
        elif words:  # short lines ("Thank you.") recur in real talk too, so only four in a row count
            same = [s for s in segments[i:i + 4] if _norm(s["text"]) == words]
            if len(same) == 4:
                flagged.update(range(i, i + 4))
        # Loops spread over several slightly different segments: look at a few neighbours together.
        j, text = i, ""
        while j < len(segments) and j < i + 8 and len(text) < 240:
            text += " " + segments[j]["text"]
            j += 1
        if _too_repetitive(text):
            flagged.update(range(i, j))
    ranges = []
    for i in sorted(flagged):
        if ranges and i <= ranges[-1][1] + 1:
            ranges[-1][1] = i
        else:
            ranges.append([i, i])
    return ranges


def _sentences(words):
    """Split a segment's words into sentences (lists of words)."""
    out, cur = [], []
    for w in words:
        cur.append(w)
        if w["w"].rstrip().endswith((".", "?", "!")):
            out.append(cur)
            cur = []
    return out + ([cur] if cur else [])


def _dedupe(segments):
    """Keep each sentence at most twice in a stretch - whatever a loop repeated beyond that goes."""
    seen = Counter()
    out = []
    for seg in segments:
        kept = []
        for sentence in _sentences(seg["words"]):
            text = "".join(w["w"] for w in sentence)
            key = " ".join(_norm(text))
            seen[key] += 1
            if not key or (seen[key] <= 2 and not _too_repetitive(text)):
                kept += sentence
        if kept:
            out.append({**seg, "words": kept, "start": kept[0]["s"], "end": kept[-1]["e"],
                        "text": "".join(w["w"] for w in kept).strip()})
    return out


def repair_loops(segments, x, regions, workdir, prompt, settings):
    """Re-transcribe looping stretches without carried-over context, then drop leftover repeats."""
    loops = find_loops(segments)
    for i0, i1 in reversed(loops):
        a = segments[i0 - 1]["end"] if i0 > 0 else 0.0
        b = segments[i1 + 1]["start"] if i1 + 1 < len(segments) else len(x) / SR
        sub = [(max(r0, a), min(r1, b)) for r0, r1 in regions if r1 > a and r0 < b]
        redo = transcribe(Timeline(x, sub), workdir, prompt, settings, fresh_context=True) if sub else []
        segments[i0:i1 + 1] = _dedupe(redo)
    return segments, len(loops)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Talks to our own whisper-server on 127.0.0.1, which urllib would otherwise send through any
# http_proxy or macOS system proxy.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class WhisperServer:
    """A long-running whisper-server so live snippets don't reload the model every time."""

    def __init__(self, settings):
        self.port = _free_port()
        self.proc = subprocess.Popen(
            [config.whisper_tool("whisper-server"), "-m", str(config.WHISPER_MODEL), "--host", "127.0.0.1",
             "--port", str(self.port), "-l", settings["language"], "-t", str(min(4, settings["threads"]))],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # The Neural Engine build compiles its encoder on a program's first start (about two minutes;
        # whisper/build.sh does it ahead of time, but a macOS update can clear that cache).
        deadline = time.time() + (300 if config.neural_engine() else 90)
        while True:
            if self.proc.poll() is not None:
                raise RuntimeError("whisper-server exited during startup")
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
                return
            except OSError:
                if time.time() > deadline:
                    self.close()
                    raise RuntimeError("whisper-server did not start")
                time.sleep(0.3)

    def transcribe(self, x, prompt=""):
        """Snippet (float32, 16 kHz) -> segments with times relative to the snippet start."""
        boundary = uuid.uuid4().hex
        body = b""
        for key, value in (("temperature", "0.0"), ("response_format", "verbose_json"), ("prompt", prompt)):
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
                 f"Content-Type: audio/wav\r\n\r\n").encode() + audio.wav_bytes(x) + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/inference", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with _LOCAL.open(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8", "replace"))
        segments = []
        for seg in result.get("segments", []):
            text = seg.get("text", "").strip()
            if not text or NON_SPEECH.match(text):
                continue
            words = [{"w": w["word"], "s": round(w["start"], 2), "e": round(w["end"], 2),
                      "p": round(w.get("probability", 1.0), 3)} for w in seg.get("words", [])]
            segments.append({"start": seg["start"], "end": seg["end"], "text": text, "words": words})
        return segments

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
