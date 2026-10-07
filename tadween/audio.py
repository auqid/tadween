"""Audio helpers built on ffmpeg. Everything internal is 16 kHz mono float32."""
import io
import json
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
    return float(info["format"]["duration"]), streams[0].get("codec_name", "")


def to_wav16k(src, dst):
    subprocess.run(
        [config.tool("ffmpeg"), "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
         "-map", "0:a:0", "-ac", "1", "-ar", str(config.SAMPLE_RATE), "-c:a", "pcm_s16le", str(dst)],
        check=True)


def make_playable(src, dst_dir):
    """An .m4a the browser can play and seek; copies AAC without re-encoding."""
    dst = Path(dst_dir) / "audio.m4a"
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
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768


def to_pcm16(x):
    return (np.clip(x, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def _write(fileobj, x):
    with wave.open(fileobj, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(config.SAMPLE_RATE)
        w.writeframes(to_pcm16(x))


def write_wav(path, x):
    _write(str(path), x)


def wav_bytes(x):
    buf = io.BytesIO()
    _write(buf, x)
    return buf.getvalue()
