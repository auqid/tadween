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
audio ─► Silero VAD ─► speech only ─► whisper.cpp large-v3-turbo (Metal GPU) ─► words + timings
          (skip silence)        │                                                     │
                                └► NeMo TitaNet voice embeddings ─► clustering ─► who said each word
                                         (2 s windows)            (+ time smoothing)     │
                                                                                         ▼
                                          remembered voices + word fixes ─► speaker-labelled transcript
```

- **Silence removal.** Whisper only sees speech, because it hallucinates during long silences. Repetition loops, a known Whisper failure, are detected and transcribed again without carried-over context.
- **Voice grouping.** Voices are grouped by clustering TitaNet embeddings, with a time-smoothing pass so the speaker doesn't flip mid-sentence. Speaker changes then snap to the nearest sentence end. TitaNet was chosen after testing on a real team call: it separated speakers far better than CAM++ or ResNet34. Voiceprints are compared as-is, so a voice saved from one call matches the same person in the next.
- **Live mode.** Audio comes from a small Swift helper ([capture/](capture/)): ScreenCaptureKit for system audio and AVAudioEngine for the mic. Snippets go to a resident `whisper-server`.

Speed on an M1 MacBook Air: about 7× faster than real time (an hour of audio in roughly 8–9 minutes).

## Setup (macOS 13+, Apple Silicon recommended)

```bash
git clone https://github.com/auqid/tadween.git
cd tadween
./setup.sh        # Homebrew ffmpeg + whisper.cpp, Python packages, ~1 GB of models, builds the capture helper
./tadween.sh      # opens http://127.0.0.1:8765
```

You need [Homebrew](https://brew.sh) and the Xcode Command Line Tools (`xcode-select --install`).

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
| `data/transcripts/<id>/` | Audio, transcript and analysis cache |
| `data/people.json` | Voiceprints |
| `data/vocabulary.json` | Word fixes |
| `data/settings.json` | Settings |

Delete a transcript in the app, or delete the folder, to remove it completely.

## Limitations

- **Speaker separation depends on the audio.** One phone recording a room is the hardest case. Live capture of the call audio is much cleaner. If Tadween splits one person into two, merge them. If it lumps people together, set the number of people.
- **Live lines appear in bursts.** Each line shows up a moment after a person pauses, not word by word.
- **Live text and labels are a draft.** The final transcript is ready a few minutes after you press Stop, about one seventh of the call's length.
- **Settings default to English.** For other languages, change the language in Settings.

## Project layout

```
tadween/      Python app: pipeline, speakers, word fixes, live sessions, local web server
web/          The interface (plain HTML/CSS/JS, no build step)
capture/      Swift helper that streams system audio / microphone as 16 kHz PCM
setup.sh      One-time setup          tadween.sh   Launcher
```

## Ideas for later

- A browser extension that reads exact participant names from Google Meet or the Zoom web client.
- Import Zoom's "separate audio file per participant" recordings for perfect speaker labels.
- Meeting summaries and action items.
