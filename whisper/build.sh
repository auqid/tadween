#!/bin/bash
# Builds whisper.cpp with Core ML so Whisper's encoder runs on the Apple Neural Engine instead of the GPU.
# Measured on an M1: recordings transcribe about 1.7x faster with the same text, and live lines appear in
# about 1.4 s instead of 3.6 s. Optional - without it Tadween uses Homebrew's whisper-cpp.
# Needs the Command Line Tools and cmake; works from any working directory; safe to run again.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

VERSION=v1.9.4
MODELS=../models
MODEL=$MODELS/ggml-large-v3-turbo-q8_0.bin
ENCODER=$MODELS/ggml-large-v3-turbo-encoder.mlmodelc  # whisper.cpp finds it next to the model

if [ "$(uname -m)" != "arm64" ]; then
  echo "    Skipped - the Neural Engine needs an Apple Silicon Mac"
  exit 0
fi

if [ "$(cat bin/VERSION 2>/dev/null)" != "$VERSION" ] || [ ! -x bin/whisper-cli ] || [ ! -x bin/whisper-server ]; then
  command -v cmake >/dev/null 2>&1 || brew install cmake
  echo "    Building whisper.cpp $VERSION with Core ML (a few minutes)"
  rm -rf src
  git clone --quiet --depth 1 --branch "$VERSION" https://github.com/ggml-org/whisper.cpp.git src
  cmake -S src -B src/build -DCMAKE_BUILD_TYPE=Release -DWHISPER_COREML=1 -DWHISPER_COREML_ALLOW_FALLBACK=1 \
    -DBUILD_SHARED_LIBS=OFF -DWHISPER_BUILD_TESTS=OFF -DGGML_METAL_EMBED_LIBRARY=ON >/dev/null
  cmake --build src/build -j "$(sysctl -n hw.ncpu)" --config Release --target whisper-cli whisper-server >/dev/null
  mkdir -p bin
  cp src/build/bin/whisper-cli src/build/bin/whisper-server bin/
  echo "$VERSION" > bin/VERSION
  rm -rf src
fi

if [ ! -d "$ENCODER" ]; then
  echo "    Downloading the Neural Engine encoder (1.2 GB)"
  curl -L --fail --progress-bar -o "$ENCODER.zip.part" \
    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-encoder.mlmodelc.zip
  unzip -q -o "$ENCODER.zip.part" -d "$MODELS"
  rm -rf "$ENCODER.zip.part" "$MODELS/__MACOSX"
fi

if [ ! -s "$MODEL" ]; then
  echo "    Skipped warm-up - $MODEL is missing (run ./setup.sh)"
  exit 0
fi

# macOS compiles the encoder for the Neural Engine the first time each program loads it (about two
# minutes, then cached). Do it now so the first transcription and the first live call start quickly.
echo "    Preparing the Neural Engine (first time only, about two minutes per program)"
warm=$(mktemp -d)
server=
trap 'rm -rf "$warm"; [ -n "$server" ] && kill "$server" 2>/dev/null || true' EXIT
python3 -c "import wave; w = wave.open('$warm/silence.wav', 'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(bytes(32000)); w.close()"
bin/whisper-cli -m "$MODEL" -f "$warm/silence.wav" -l en -nt >/dev/null 2>&1
port=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
bin/whisper-server -m "$MODEL" --host 127.0.0.1 --port "$port" >/dev/null 2>&1 &
server=$!
for _ in $(seq 600); do
  curl -s -o /dev/null "http://127.0.0.1:$port/" && break
  kill -0 "$server" 2>/dev/null || { echo "    whisper-server exited during warm-up" >&2; exit 1; }
  sleep 1
done

echo "Built $(pwd)/bin (whisper.cpp $VERSION with Core ML)"
