#!/bin/bash
# One-time setup for Tadween on macOS: tools, Python packages, models (~1 GB) and the capture helper.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required: https://brew.sh" >&2
  exit 1
fi

echo "==> ffmpeg and whisper.cpp (Homebrew)"
brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
brew list whisper-cpp >/dev/null 2>&1 || brew install whisper-cpp

echo "==> Python packages"
if ! .venv/bin/python -c '' 2>/dev/null; then  # missing, or its Python was removed by a Homebrew upgrade
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

echo
echo "Done. Start Tadween with:  ./tadween.sh"
