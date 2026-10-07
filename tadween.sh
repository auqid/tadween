#!/bin/bash
# ./tadween                               open the app in your browser
# ./tadween transcribe FILE [--speakers N] write "FILE - transcript.txt" next to the recording
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -x "$DIR/.venv/bin/python" ]; then
  echo "Run $DIR/setup.sh first." >&2
  exit 1
fi
if [ $# -eq 0 ] || [[ $1 == -* ]]; then set -- serve "$@"; fi  # ./tadween.sh --port 8766 means serve
PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}" exec "$DIR/.venv/bin/python" -m tadween "$@"
