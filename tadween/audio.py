"""Audio helpers built on ffmpeg. Everything internal is 16 kHz mono float32."""
import io
import json
import re
import subprocess
import wave
from pathlib import Path

import numpy as np

from . import config


def probe(path):
    """Duration in seconds and the codec of the first audio stream."""
    out = subprocess.run(
        [config.tool("ffprobe"), "-v", "error", "-select_streams", "a:0",
         "-show_entries", "format=duration:stream=codec_name", "-of", "json", str(path)],
        capture_output=True, text=True, check=True)
    info = json.loads(out.stdout)
    streams = info.get("streams") or []
    if not streams:
        raise ValueError("This file has no audio track.")
    duration = (info.get("format") or {}).get("duration")
    if duration is None:  # a streamed WebM, as browsers record, has no length in its header: read to the end
        out = subprocess.run(
            [config.tool("ffmpeg"), "-nostdin", "-loglevel", "error", "-i", str(path), "-map", "0:a:0",
             "-c", "copy", "-f", "null", "-progress", "pipe:1", "-"],
            capture_output=True, text=True, check=True)
        duration = int((re.findall(r"^out_time_us=(\d+)", out.stdout, re.M) or [0])[-1]) / 1e6
    return float(duration), streams[0].get("codec_name", "")


def to_wav16k(src, dst):
    subprocess.run(
        [config.tool("ffmpeg"), "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
         # Pad audio that starts late (after a video's first frame) so word times match the player's clock.
         # Gaps later on stay closed: Chrome and Safari play straight through them.
         "-af", "aresample=async=1:first_pts=0:min_hard_comp=86400",
         "-map", "0:a:0", "-ac", "1", "-ar", str(config.SAMPLE_RATE), "-c:a", "pcm_s16le", str(dst)],
        check=True)


def make_playable(src, dst_dir):
    """An .m4a the browser can play and seek; copies AAC without re-encoding. A list of tracks is mixed."""
    dst = Path(dst_dir) / "audio.m4a"
    if isinstance(src, (list, tuple)) and len(src) > 1:  # a live call's mic and call audio, summed like one room
        inputs = [arg for s in src for arg in ("-i", str(s))]
        subprocess.run(
            [config.tool("ffmpeg"), "-nostdin", "-loglevel", "error", "-y", *inputs,
             "-filter_complex", f"amix=inputs={len(src)}:duration=longest:normalize=0",
             "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(dst)],
            check=True)
        return dst
    src = src[0] if isinstance(src, (list, tuple)) else src
    _, codec = probe(src)
    codec_args = ["-c:a", "copy"] if codec == "aac" else ["-c:a", "aac", "-b:a", "64k"]
    subprocess.run(
        [config.tool("ffmpeg"), "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
         "-vn", "-map", "0:a:0", *codec_args, "-movflags", "+faststart", str(dst)],
        check=True)
    return dst


def read_wav(path):
    with wave.open(str(path)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (config.SAMPLE_RATE, 1, 2):
            raise ValueError(f"{path} is not 16 kHz mono 16-bit")
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
    x *= 1 / 32768  # in place: another full-size copy of an hour-long call would cost 230 MB more
    return x


def to_pcm16(x):
    return (np.clip(x, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def _write(fileobj, x):
    """x: one array, or an iterable of arrays written one after another."""
    with wave.open(fileobj, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(config.SAMPLE_RATE)
        for piece in [x] if isinstance(x, np.ndarray) else x:
            w.writeframes(to_pcm16(piece))


def write_wav(path, x):
    _write(str(path), x)


def wav_bytes(x):
    buf = io.BytesIO()
    _write(buf, x)
    return buf.getvalue()
