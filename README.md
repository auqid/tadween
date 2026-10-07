# Tadween

**Private call transcripts with speaker names, on your Mac.** *Tadween* (تدوين) means "recording / writing down".

Tadween transcribes Zoom, Meet, Teams or any other calls, live or from a recording. It works out who is talking and learns their voices, so names stick from one call to the next. When you correct a word, it offers to fix it everywhere, including in future calls. Everything runs locally: audio never leaves the machine.

## What it does

- **Transcribe recordings.** Drop in an `.m4a`, `.mp3`, `.mp4`, `.mov` or `.wav` file. It gets a timestamped transcript split into speaker turns.
- **Live calls.** Your microphone is labelled as you, and the Mac's sound output (everyone else) is split by voice as people talk. When you stop, the whole call is transcribed again with full context and the voices are regrouped, so the saved transcript is as good as a recording's. This works with any meeting app, because it captures audio rather than reading a web page.
- **Names that stick.** Click *Speaker 3*, type "Sara", and keep "remember this voice" ticked. Tadween stores a voiceprint (a vector describing the voice, not audio) and labels Sara automatically in later calls.
- **Fix once, fixed everywhere.** Edit a word, or select it and click *Fix everywhere*. Tadween shows how often it occurs and replaces every occurrence. Remembered fixes are applied to future transcripts and also passed to Whisper, so it spells the word correctly to begin with.
- **Easy cleanup:**
  - Merge two speakers who are really one person.
  - Move a line to another speaker.
  - Regroup the voices with a known number of people.
  - Play a sample of a speaker's voice to identify them.
  - Words Whisper was unsure of are underlined, so you know where to look.
- **Export** to `.txt`, `.md`, `.srt` or `.vtt`.

## How it works

```
audio ─► Silero VAD ─► speech only ─► whisper.cpp large-v3-turbo (Neural Engine + GPU) ─► words + timings
          (skip silence)        │                                                     │
                                └► NeMo TitaNet voice embeddings ─► clustering ─► who said each word
                                         (2 s windows)            (+ time smoothing)     │
                                                                                         ▼
                                          remembered voices + word fixes ─► speaker-labelled transcript
```

- **Silence removal.** Whisper only sees speech, because it hallucinates during long silences. Repetition loops, a known Whisper failure, are detected and transcribed again without carried-over context.
- **Voice grouping.** Voices are grouped by clustering TitaNet embeddings, with a time-smoothing pass so the speaker doesn't flip mid-sentence. Speaker changes then snap to the nearest sentence end. TitaNet was chosen after testing on a real team call: it separated speakers far better than CAM++ or ResNet34. Voiceprints are compared as-is, so a voice saved from one call matches the same person in the next.
- **Live mode.** Audio comes from a small Swift helper ([capture/](capture/)): ScreenCaptureKit for system audio and AVAudioEngine for the mic. Snippets go to a resident `whisper-server`.
- **Neural Engine.** `setup.sh` builds whisper.cpp with Core ML ([whisper/build.sh](whisper/build.sh)), so Whisper's encoder runs on the Apple Neural Engine instead of the GPU. Without it, Tadween uses Homebrew's whisper-cpp.

Speed on an M1 MacBook Air (8 GB), measured on a real team call:

| | GPU only | With the Neural Engine |
|---|---|---|
| Whisper, 8.4 min of speech | 58 s | 33 s, same text and timings |
| Whole pipeline, 10-minute clip | 71 s | 43 s |
| A live line appears after the speaker pauses | 3.6 s | 1.4 s |

*Settings → Transcription speed → Faster* switches to greedy decoding: about a quarter quicker again (26 s instead of 33 s for the Whisper step above). It drops most filler words ("um", "uh") and may word a few phrases differently.

## Setup (macOS 13+, Apple Silicon recommended)

```bash
git clone https://github.com/auqid/tadween.git
cd tadween
./setup.sh        # Homebrew ffmpeg + whisper.cpp, Python packages, ~1 GB of models, the capture helper,
                  # and the Neural Engine build of whisper.cpp (1.2 GB more, a few minutes once)
./tadween.sh      # opens http://127.0.0.1:8765
```

You need [Homebrew](https://brew.sh) and the Xcode Command Line Tools (`xcode-select --install`). The Neural Engine build also needs `cmake`, which setup installs with Homebrew. macOS prepares the Neural Engine model the first time each program uses it (about two minutes); setup does that for you, and after a macOS update the first transcription or live call may take that long to start.

### Models

The models are not in this repository (about 2 GB in all). `./setup.sh` downloads them into `models/`, and its last step (`./whisper/build.sh`) adds the Neural Engine encoder. Both skip files that are already there, so if a download fails, just run `./setup.sh` again.

| File in `models/` | What it does | Size | Source | License |
|---|---|---|---|---|
| `ggml-large-v3-turbo-q8_0.bin` | Whisper large-v3-turbo, 8-bit: speech to text | 874 MB | [ggerganov/whisper.cpp](https://huggingface.co/ggerganov/whisper.cpp) | MIT |
| `silero_vad.onnx` | Silero VAD: finds where people speak | 0.6 MB | [sherpa-onnx `asr-models`](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models) | MIT |
| `nemo_en_titanet_large.onnx` | NVIDIA NeMo TitaNet-large: voiceprints | 101 MB | [sherpa-onnx `speaker-recongition-models`](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models) | CC BY 4.0 |
| `ggml-large-v3-turbo-encoder.mlmodelc/` | Whisper's encoder for the Neural Engine (optional) | 1.2 GB | [ggerganov/whisper.cpp](https://huggingface.co/ggerganov/whisper.cpp) | MIT |

To download them by hand instead:

```bash
mkdir -p models && cd models
curl -LO https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q8_0.bin
curl -LO https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx
curl -LO https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_large.onnx

# Optional, for the Neural Engine:
curl -LO https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-encoder.mlmodelc.zip
unzip -q ggml-large-v3-turbo-encoder.mlmodelc.zip && rm -rf ggml-large-v3-turbo-encoder.mlmodelc.zip __MACOSX
cd .. && ./whisper/build.sh
```

Keep the file names: Tadween looks for exactly these, and whisper.cpp finds the encoder by its name next to the Whisper model. The encoder is used only by the Core ML build of whisper.cpp, which `./whisper/build.sh` makes; it skips the download when the encoder is already in `models/`. (The release tag really is spelled "recongition".)

**Live-call permissions.** The first time you start a live session, macOS asks for **Microphone** and **Screen & System Audio Recording** access. Both go to the app that launched Tadween, such as Terminal or VS Code. Allow both, then quit and reopen that app. Headphones give the cleanest result. Without them, Tadween drops mic lines that are just an echo of the call.

## Command line

```bash
./tadween.sh transcribe "Team sync.m4a"                # writes "Team sync - transcript.txt" next to it
./tadween.sh transcribe call.mp4 --speakers 5 --format md
```

## Your data

Everything lives in `data/`, which git ignores:

| Path | Contents |
|---|---|
| `data/transcripts/<id>/` | Playable audio, transcript and analysis cache |
| `data/people.json` | Voiceprints |
| `data/vocabulary.json` | Word fixes |
| `data/settings.json` | Settings |

Delete a transcript in the app, or delete the folder, to remove it completely.

## Limitations

- **Speaker separation depends on the audio.** One phone recording a room is the hardest case. Live capture of the call audio is much cleaner. If Tadween splits one person into two, merge them. If it lumps people together, set the number of people.
- **Live lines appear in bursts.** Each line shows up a moment after a person pauses, not word by word.
- **Live mode assumes your mic is only you.** If the call audio stays silent (people in the room, or a call on another device), the live view labels everything your mic hears as you and says so. The final transcript then tells the voices on your mic apart, as for a recording.
- **Live text and labels are a draft.** After you press Stop, the whole call is transcribed again for the final version. With the Neural Engine this takes about a tenth of the call's length.
- **Settings default to English.** For other languages, change the language in Settings.

## Project layout

```
tadween/      Python app: pipeline, speakers, word fixes, live sessions, local web server
web/          The interface (plain HTML/CSS/JS, no build step)
capture/      Swift helper that streams system audio / microphone as 16 kHz PCM
whisper/      Builds whisper.cpp with Core ML for the Neural Engine (build.sh)
setup.sh      One-time setup          tadween.sh   Launcher
```

## Ideas for later

- A browser extension that reads exact participant names from Google Meet or the Zoom web client.
- Import Zoom's "separate audio file per participant" recordings for perfect speaker labels.
- Meeting summaries and action items.
