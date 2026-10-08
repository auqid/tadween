"""Where Tadween keeps its models, data and settings."""
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

PLATFORM = "mac" if sys.platform == "darwin" else "windows" if os.name == "nt" else "linux"
ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
WEB = ROOT / "web"
DATA = Path(os.environ.get("TADWEEN_DATA", ROOT / "data"))
TRANSCRIPTS = DATA / "transcripts"
CAPTURE_BIN = ROOT / "capture" / "tadween-capture"  # macOS live capture; Windows and Linux use tadween/capture.py
TOOLS = ROOT / "tools"  # ffmpeg downloaded by setup.ps1 on Windows

SAMPLE_RATE = 16000

WHISPER_MODEL = MODELS / "ggml-large-v3-turbo-q8_0.bin"
WHISPER_DTW_PRESET = "large.v3.turbo"
# whisper.cpp built by whisper/build.sh (Core ML on a Mac, CUDA or CPU on Linux) or unpacked by setup.ps1 (Windows).
# The Core ML build finds the encoder next to the model by name.
WHISPER_BIN = ROOT / "whisper" / "bin"
COREML_ENCODER = MODELS / "ggml-large-v3-turbo-encoder.mlmodelc"
VAD_MODEL = MODELS / "silero_vad.onnx"
# NeMo TitaNet-large: on a real team call it separated voices far better than CAM++ or ResNet34.
SPEAKER_MODEL = MODELS / "nemo_en_titanet_large.onnx"

LANGUAGES = {"en": "English", "auto": "Detect automatically", "ar": "Arabic", "hi": "Hindi", "ur": "Urdu",
             "es": "Spanish", "fr": "French", "de": "German"}

DEFAULT_SETTINGS = {
    "my_name": "Me",
    "language": "en",
    # On a Mac, Whisper runs on the GPU / Neural Engine and more CPU threads only compete with voice recognition
    # (M1: 4 beat 6). Elsewhere Whisper may run on the CPU: one thread per core (half the logical CPUs), up to 8.
    "threads": 4 if PLATFORM == "mac" else max(4, min(8, (os.cpu_count() or 8) // 2)),
    # Cosine distance for grouping voices: higher merges more voices together. 0.8 kept every known
    # speaker together and apart from the others, on 10-minute clips and an 80-minute call alike.
    "speaker_threshold": 0.8,
    # Similarity a speaker must reach to be auto-named from a remembered voice (same person ~0.7, others <0.4).
    "voice_match_threshold": 0.55,
    # Punctuated opening that nudges Whisper to write punctuation and capitals.
    "initial_prompt": "Hi everyone, welcome to the call.",
    # "accurate": beam search. "fast": greedy decoding - about a quarter quicker, drops most filler words.
    "speed": "accurate",
}

_lock = threading.Lock()
SETTINGS_FILE = DATA / "settings.json"


def _exe(folder, name):
    for candidate in (folder / name, folder / f"{name}.exe"):
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def tool(name):
    """Find ffmpeg, whisper-cli and friends: on PATH, in Tadween's tools/ folder (Windows), or where Homebrew
    puts them even when PATH is minimal (e.g. launched from an app)."""
    found = shutil.which(name) or _exe(TOOLS, name)
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
        found = found or _exe(Path(prefix), name)
    if found:
        return found
    raise FileNotFoundError(f"{name} not found - run {'setup.ps1' if PLATFORM == 'windows' else './setup.sh'}")


def neural_engine_installed():
    return PLATFORM == "mac" and (WHISPER_BIN / "whisper-cli").exists() and COREML_ENCODER.exists()


def neural_engine():
    """Use the Core ML build? Whisper's encoder then runs on the Neural Engine (~1.7x faster on an M1).
    TADWEEN_NEURAL_ENGINE=0 turns it off, to compare with the GPU (big GPUs on Max/Ultra chips may win)."""
    return os.environ.get("TADWEEN_NEURAL_ENGINE") != "0" and neural_engine_installed()


def whisper_build():
    """What setup put in whisper/bin: "cuda", "cpu" or "coreml" (None if nothing, e.g. Homebrew's on a Mac)."""
    try:
        version = (WHISPER_BIN / "VERSION").read_text(encoding="utf-8").lower()
    except OSError:
        return None
    return "cuda" if "cuda" in version else "coreml" if "coreml" in version else "cpu"


def whisper_tool(name):
    """whisper-cli or whisper-server: our own build in whisper/bin, or else the one on PATH (Homebrew's on a Mac).
    On a Mac whisper/bin holds the Core ML build, which is skipped when the Neural Engine is turned off."""
    if PLATFORM == "mac" and not neural_engine():
        return tool(name)
    return _exe(WHISPER_BIN, name) or tool(name)  # resolved: macOS caches the Neural Engine compile per path


def read_json(path, default):
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except ValueError:  # empty or damaged: set it aside rather than stop the app, and never write over it
        aside = path.with_name(path.name + ".damaged")
        os.replace(path, aside)
        print(f"Tadween: {path} could not be read; moved it to {aside.name}", file=sys.stderr)
        return default


def write_json(path, obj):
    """Atomic write so a crash never leaves half a file behind."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False))
        f.flush()
        os.fsync(f.fileno())  # on disk before it replaces the old file, even if power is lost
    for attempt in range(40):
        try:
            return os.replace(tmp, path)
        except PermissionError:  # Windows: someone is reading the old file right now
            if PLATFORM != "windows" or attempt == 39:
                raise
            time.sleep(0.05)


def remove(path):
    """Delete a leftover file. On Windows a virus scanner can still have a new file open: retry for a moment,
    then leave the file rather than fail the job over it."""
    for _ in range(20):
        try:
            return Path(path).unlink(missing_ok=True)
        except PermissionError:
            if PLATFORM != "windows":
                raise
            time.sleep(0.05)


def _valid(key, value):
    """A setting's value as stored, or ValueError (the API answers 400) if it can't be used."""
    def number(kind, lo, hi):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi \
                or (kind is int and value != int(value)):
            raise ValueError(f"{key} must be a {'whole ' if kind is int else ''}number from {lo} to {hi}")
        return kind(value)

    if key == "threads":
        return number(int, 1, 16)
    if key == "speaker_threshold":
        return number(float, 0.5, 1.0)
    if key == "voice_match_threshold":
        return number(float, 0.3, 0.8)
    if key == "language":
        if value not in LANGUAGES:
            raise ValueError(f"Unknown language {value!r}")
        return value
    if key == "speed":
        if value not in ("accurate", "fast"):
            raise ValueError("speed must be 'accurate' or 'fast'")
        return value
    if not isinstance(value, str):  # my_name, initial_prompt
        raise ValueError(f"{key} must be text")
    return value.strip()[:600]


def load_settings():
    with _lock:
        return {**DEFAULT_SETTINGS, **read_json(SETTINGS_FILE, {})}


def save_settings(update):
    with _lock:
        current = {**DEFAULT_SETTINGS, **read_json(SETTINGS_FILE, {})}
        current.update({k: _valid(k, v) for k, v in update.items() if k in DEFAULT_SETTINGS})
        write_json(SETTINGS_FILE, current)
        return current
