"""Which engine runs Whisper fastest on this computer, and is the large model quick enough there for live lines?

Timed after setup and from Settings > Check speed again: 30 seconds of audio on each engine this computer has, the
Neural Engine and the GPU on a Mac, the GPU and the CPU elsewhere. A weak integrated GPU can lose to the CPU and a
graphics card short of memory can fail, so the fastest one that works wins. Where even that needs more than
LIVE_OK_MS, live lines use the small model; transcripts always use the large one. Saved in data/speed.json, which
config.engine() and config.live_model() follow unless Settings say otherwise.
"""
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

from . import audio, config, models

SECONDS = 30  # one Whisper window: the encoder, where nearly all the time goes, always works on 30 s of audio
SLOWER_OK = 1.1  # off a Mac, a GPU up to 10% slower still beats the CPU: it leaves the CPU to the call and voices
LIVE_OK_MS = 4000  # the large model's 30 s on the chosen engine; any slower and live lines trail far behind
NAMES = {"neural_engine": "the Neural Engine", "gpu": "the GPU", "cpu": "the CPU"}
running = threading.Lock()  # one check at a time


def _candidates():
    """engine -> (whisper-cli, extra flags), for each engine this computer has."""
    if config.PLATFORM == "mac":
        found = {}
        if config.neural_engine_installed():
            found["neural_engine"] = (str((config.WHISPER_BIN / "whisper-cli").resolve()), [])
        try:
            found["gpu"] = (config.tool("whisper-cli"), [])  # Homebrew's whisper-cpp, on Metal
        except FileNotFoundError:
            pass
        return found
    cli = config.whisper_tool("whisper-cli")
    if config.whisper_build() in ("cuda", "vulkan"):
        return {"gpu": (cli, []), "cpu": (cli, ["-ng"])}
    return {"cpu": (cli, [])}


def _time(cli, flags, wav, expect_gpu):
    """One run -> (encode ms, None), or (None, why it failed)."""
    cmd = [cli, "-m", str(config.WHISPER_MODEL), "-f", str(wav), "-l", "en", "-t", str(config.threads()), "-nt", *flags]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=900,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return None, "took over 15 minutes"
    encode = re.search(r"encode time =\s*([\d.]+) ms", proc.stderr)
    if proc.returncode != 0 or not encode:
        tail = [line.strip() for line in proc.stderr.splitlines() if line.strip()][-2:]
        return None, " ".join(tail) or f"exit code {proc.returncode}"
    if expect_gpu and not re.search(r"whisper_backend_init_gpu: using \S+ backend", proc.stderr):
        return None, "whisper.cpp found no GPU it can use"
    return float(encode.group(1)), None


def check(say=print):
    """Time each engine, pick the fastest and the live model, save them. Downloads the small model if it's picked."""
    if not running.acquire(blocking=False):
        raise RuntimeError("A speed check is already running.")
    try:
        return _check(say)
    finally:
        running.release()


def start(say, done):
    """For the app: claim the check at once (a second request is refused), run it in the background, then
    done(result, error)."""
    if not running.acquire(blocking=False):
        raise RuntimeError("A speed check is already running.")

    def work():
        try:
            result, error = _check(say), None
        except Exception as e:
            result, error = None, e
        finally:
            running.release()
        done(result, error)

    threading.Thread(target=work, daemon=True, name="tadween-speed-check").start()


def _check(say):
    candidates = _candidates()
    if not candidates:
        raise RuntimeError("whisper.cpp isn't installed: run setup")
    say(f"Timing Whisper on {' and '.join(NAMES[e] for e in candidates)}: {SECONDS} s of audio each, about a minute")
    times, errors = {}, {}
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "hiss.wav"
        hiss = np.random.default_rng(0).normal(0, 1e-3, SECONDS * config.SAMPLE_RATE).astype(np.float32)
        audio.write_wav(wav, [hiss])
        for name, (cli, flags) in candidates.items():
            expect_gpu = config.PLATFORM != "mac" and name == "gpu"
            if name != "cpu":  # a GPU's first run compiles its shaders, the Neural Engine's its model: time the next
                _time(cli, flags, wav, expect_gpu)
            times[name], errors[name] = _time(cli, flags, wav, expect_gpu)
            say(f"  {NAMES[name]}: {seconds(times[name]) if times[name] else 'failed - ' + errors[name]}")
    working = {name: ms for name, ms in times.items() if ms}
    if config.PLATFORM != "mac" and "gpu" in working and working["gpu"] <= working.get("cpu", 1e12) * SLOWER_OK:
        engine = "gpu"
    else:
        engine = min(working, key=working.get) if working else next(iter(candidates), "cpu")
    result = {"whisper": config.whisper_build_id(), "engine": engine,
              "live_model": "small" if working.get(engine, 0) > LIVE_OK_MS else "large",
              "times": {name: round(ms) if ms else None for name, ms in times.items()},
              "errors": {name: error for name, error in errors.items() if error}, "checked": round(time.time())}
    config.write_json(config.SPEED_FILE, result)
    if result["live_model"] == "small" and not config.SMALL_MODEL.exists():
        say(f"Downloading the small model for live lines ({models.SMALL_MB} MB)")
        shown = [0.0]

        def progress(fraction):
            if fraction - shown[0] >= 0.25:
                shown[0] = fraction
                say(f"  {int(fraction * 100)}%")
        try:
            models.fetch_small(progress)
        except Exception as e:  # the timings stand; live lines use the large model until the download works
            say(f"  The download failed ({type(e).__name__}: {e}). Live lines use the large model until it works.")
    return result


def seconds(ms):
    return f"{ms / 1000:.1f} s"


def describe(result):
    times = ", ".join(f"{NAMES[name]} {seconds(ms) if ms else 'failed'}" for name, ms in result["times"].items())
    live = ("the large model" if result["live_model"] == "large"
            else "the small model, as the large one is too slow here for live lines")
    return f"Whisper will run on {NAMES[result['engine']]} ({times} for {SECONDS} s of audio). Live lines: {live}."
