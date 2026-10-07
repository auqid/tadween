"""Where Tadween keeps its models, data and settings."""
import json
import os
import shutil
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
WEB = ROOT / "web"
DATA = Path(os.environ.get("TADWEEN_DATA", ROOT / "data"))
TRANSCRIPTS = DATA / "transcripts"
CAPTURE_BIN = ROOT / "capture" / "tadween-capture"

SAMPLE_RATE = 16000

WHISPER_MODEL = MODELS / "ggml-large-v3-turbo-q8_0.bin"
WHISPER_DTW_PRESET = "large.v3.turbo"
VAD_MODEL = MODELS / "silero_vad.onnx"
# NeMo TitaNet-large: on a real team call it separated voices far better than CAM++ or ResNet34.
SPEAKER_MODEL = MODELS / "nemo_en_titanet_large.onnx"

DEFAULT_SETTINGS = {
    "my_name": "Me",
    "language": "en",
    "threads": 6,
    # Cosine distance for grouping voices: higher merges more voices together. 0.8 kept every known
    # speaker together and apart from the others, on 10-minute clips and an 80-minute call alike.
    "speaker_threshold": 0.8,
    # Similarity a speaker must reach to be auto-named from a remembered voice (same person ~0.7, others <0.4).
    "voice_match_threshold": 0.55,
    # Punctuated opening that nudges Whisper to write punctuation and capitals.
    "initial_prompt": "Hi everyone, welcome to the call.",
}

_lock = threading.Lock()
SETTINGS_FILE = DATA / "settings.json"


def tool(name):
    """Find a Homebrew binary even when PATH is minimal (e.g. launched from an app)."""
    found = shutil.which(name)
    if found:
        return found
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
        candidate = Path(prefix) / name
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(f"{name} not found - run ./setup.sh")


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path, obj):
    """Atomic write so a crash never leaves half a file behind."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_settings():
    with _lock:
        return {**DEFAULT_SETTINGS, **read_json(SETTINGS_FILE, {})}


def save_settings(update):
    with _lock:
        current = {**DEFAULT_SETTINGS, **read_json(SETTINGS_FILE, {})}
        current.update({k: v for k, v in update.items() if k in DEFAULT_SETTINGS})
        write_json(SETTINGS_FILE, current)
        return current
