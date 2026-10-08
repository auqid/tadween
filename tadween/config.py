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
SPEED_FILE = DATA / "speed.json"  # what `tadween speed-check` measured on this computer (speed.py)
CAPTURE_BIN = ROOT / "capture" / "tadween-capture"  # macOS live capture; Windows and Linux use tadween/capture.py
TOOLS = ROOT / "tools"  # ffmpeg downloaded by setup.ps1 on Windows

SAMPLE_RATE = 16000

WHISPER_MODEL = MODELS / "ggml-large-v3-turbo-q8_0.bin"
WHISPER_DTW_PRESET = "large.v3.turbo"
# Live lines on a computer too slow for the large model (Settings > Model for live lines): about a seventh of the
# work, drafts a little rougher. Transcripts always use the large model. Downloaded only when it's wanted.
SMALL_MODEL = MODELS / "ggml-small-q8_0.bin"
SMALL_MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small-q8_0.bin"
# whisper.cpp built by whisper/build.sh (Core ML on a Mac; CUDA, Vulkan or CPU on Linux) or unpacked by setup.ps1
# (Windows), all from the release in whisper/RELEASE.
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
    # CPU threads for Whisper and voice recognition: "auto" (hardware.auto_threads) or a number.
    "threads": "auto",
    # Where Whisper runs: "auto" (what speed-check found fastest), "neural_engine" or "gpu" on a Mac, "gpu" or "cpu"
    # elsewhere. And the model for live lines: "auto" (small only where large is too slow for live), "large", "small".
    "engine": "auto",
    "live_model": "auto",
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


def whisper_build():
    """What setup put in whisper/bin: "cuda", "vulkan", "cpu" or "coreml" (None if nothing, e.g. Homebrew's)."""
    try:
        version = (WHISPER_BIN / "VERSION").read_text(encoding="utf-8").lower()
    except OSError:
        return None
    return next((kind for kind in ("cuda", "vulkan", "coreml") if kind in version), "cpu")


def whisper_build_id():
    """The whisper.cpp in use, as a speed check records it, so a rebuild makes the check out of date."""
    try:
        return (WHISPER_BIN / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "PATH"


def speed_check():
    """What `tadween speed-check` last measured here (speed.py), or None if nothing for this whisper.cpp."""
    measured = read_json(SPEED_FILE, None)
    if isinstance(measured, dict) and measured.get("whisper") == whisper_build_id() and measured.get("engine"):
        return measured
    return None


def engines():
    """Where Whisper can run here: the Neural Engine and/or the GPU on a Mac; the GPU and/or the CPU elsewhere."""
    if PLATFORM == "mac":
        return (["neural_engine"] if neural_engine_installed() else []) + ["gpu"]
    return ["gpu", "cpu"] if whisper_build() in ("cuda", "vulkan") else ["cpu"]


def engine(settings=None):
    """Where Whisper runs: TADWEEN_NEURAL_ENGINE=0 (Mac) or TADWEEN_GPU=0/1, else Settings, else what speed-check
    found fastest, else the Neural Engine on a Mac (~1.7x the GPU on an M1) or the GPU when there is one."""
    possible = engines()
    if PLATFORM == "mac":
        forced = "gpu" if os.environ.get("TADWEEN_NEURAL_ENGINE") == "0" else None
    else:
        forced = {"0": "cpu", "1": "gpu"}.get(os.environ.get("TADWEEN_GPU"))
    chosen = (settings or load_settings())["engine"]
    measured = (speed_check() or {}).get("engine")
    return next((e for e in (forced, chosen, measured) if e in possible), possible[0])


def neural_engine():
    """Is Whisper's encoder running on the Neural Engine (the Core ML build in whisper/bin)?"""
    return engine() == "neural_engine"


def whisper_gpu_args():
    """-ng (no GPU) when a GPU build of whisper.cpp should run on the CPU. Never on a Mac."""
    if PLATFORM != "mac" and whisper_build() in ("cuda", "vulkan") and engine() == "cpu":
        return ["-ng"]
    return []


def whisper_tool(name):
    """whisper-cli or whisper-server: our own build in whisper/bin, or else the one on PATH (Homebrew's on a Mac).
    On a Mac whisper/bin holds the Core ML build, used only when the engine is the Neural Engine."""
    if PLATFORM == "mac" and not neural_engine():
        return tool(name)
    return _exe(WHISPER_BIN, name) or tool(name)  # resolved: macOS caches the Neural Engine compile per path


def live_model(settings=None):
    """The model for live lines: Settings, else what speed-check chose, else large. Small once it's downloaded."""
    chosen = (settings or load_settings())["live_model"]
    if chosen == "auto":
        chosen = (speed_check() or {}).get("live_model", "large")
    return SMALL_MODEL if chosen == "small" and SMALL_MODEL.exists() else WHISPER_MODEL


def threads(settings=None):
    """CPU threads for Whisper and voice recognition: the number in Settings, or one per performance core."""
    value = (settings or load_settings())["threads"]
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    from . import hardware  # it imports this module
    return hardware.auto_threads()


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
        return "auto" if value == "auto" else number(int, 1, 16)
    if key in ("engine", "live_model"):
        allowed = ("auto", "neural_engine", "gpu", "cpu") if key == "engine" else ("auto", "large", "small")
        if value not in allowed:
            raise ValueError(f"{key} must be one of: {', '.join(allowed)}")
        return value
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
