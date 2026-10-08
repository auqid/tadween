#!/bin/bash
# One-time setup for Tadween on macOS or Linux: tools, Python packages, models (~1 GB), whisper.cpp and,
# on a Mac, the live-call capture helper. (Windows: setup.ps1.) Safe to run again.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
OS=$(uname -s)

if [ "$OS" = "Darwin" ]; then
  if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew is required: https://brew.sh" >&2
    exit 1
  fi
  echo "==> ffmpeg and whisper.cpp (Homebrew)"
  brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
  brew list whisper-cpp >/dev/null 2>&1 || brew install whisper-cpp
else
  echo "==> Checking tools"
  missing=()
  for t in python3 ffmpeg ffprobe git cmake c++ curl; do command -v "$t" >/dev/null 2>&1 || missing+=("$t"); done
  python3 -c 'import sys, venv, ensurepip; sys.exit(sys.version_info < (3, 10))' 2>/dev/null || missing+=("python3.10+ with venv")
  if [ ${#missing[@]} -gt 0 ]; then
    echo "Missing: ${missing[*]}. Install them, then run ./setup.sh again:" >&2
    echo "  Debian/Ubuntu:  sudo apt install python3 python3-venv ffmpeg git cmake g++ curl libpulse0" >&2
    echo "  Fedora:         sudo dnf install python3 ffmpeg git cmake gcc-c++ curl pulseaudio-libs  (ffmpeg is in RPM Fusion)" >&2
    echo "  Arch:           sudo pacman -S python ffmpeg git cmake gcc curl libpulse" >&2
    exit 1
  fi
fi

echo "==> Python packages"
if ! .venv/bin/python -c '' 2>/dev/null; then  # missing, or its Python was removed by an upgrade
  rm -rf .venv
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt

echo "==> Models (downloaded once)"
mkdir -p models
fetch() {  # url destination
  [ -s "$2" ] && return 0
  echo "    $2"
  curl -L --fail --progress-bar -o "$2.part" "$1"
  mv "$2.part" "$2"
}
fetch https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q8_0.bin models/ggml-large-v3-turbo-q8_0.bin
fetch https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx models/silero_vad.onnx
# (the release tag really is spelled "recongition")
fetch https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_large.onnx models/nemo_en_titanet_large.onnx

if [ "$OS" = "Darwin" ]; then
  echo "==> Audio capture helper (for live calls)"
  if command -v swiftc >/dev/null 2>&1; then
    ./capture/build.sh
  else
    echo "    Skipped - install the Xcode Command Line Tools (xcode-select --install), then run capture/build.sh"
  fi

  echo "==> Faster transcription on the Neural Engine (Apple Silicon; 1.2 GB download, a few minutes once)"
  if ! ./whisper/build.sh; then
    echo "    Skipped - Tadween will use Homebrew's whisper-cpp. Run ./whisper/build.sh later to try again."
  fi
else
  echo "==> whisper.cpp, built for this computer's GPU (CUDA or Vulkan) or its CPU"
  ./whisper/build.sh
fi

echo "==> Speed check: where Whisper runs fastest here (Settings can change it)"
.venv/bin/python -m tadween speed-check --if-needed

echo
echo "Done. Start Tadween with:  ./tadween.sh"
