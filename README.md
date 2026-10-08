# Tadween

**Private call transcripts with speaker names, on your own computer: macOS, Windows or Linux.** *Tadween* (تدوين) means "recording / writing down".

Tadween transcribes Zoom, Meet, Teams or any other calls, live or from a recording. It works out who is talking and learns their voices, so names stick from one call to the next. When you correct a word, it offers to fix it everywhere, including in future calls. Everything runs locally: audio never leaves the machine.

## What it does

- **Transcribe recordings.** Drop in an `.m4a`, `.mp3`, `.mp4`, `.mov` or `.wav` file. It gets a timestamped transcript split into speaker turns.
- **Live calls.** Your microphone is labelled as you, and the computer's sound output (everyone else) is split by voice as people talk. When you stop, the whole call is transcribed again with full context and the voices are regrouped, so the saved transcript is as good as a recording's. This works with any meeting app, because it captures audio rather than reading a web page.
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
audio ─► Silero VAD ─► speech only ─► whisper.cpp large-v3-turbo (Neural Engine, GPU or CPU) ─► words + timings
          (skip silence)        │                                                           │
                                └► NeMo TitaNet voice embeddings ─► clustering ─► who said each word
                                         (2 s windows)            (+ time smoothing)     │
                                                                                         ▼
                                          remembered voices + word fixes ─► speaker-labelled transcript
```

- **Silence removal.** Whisper only sees speech, because it hallucinates during long silences. Repetition loops, a known Whisper failure, are detected and transcribed again without carried-over context.
- **Voice grouping.** Voices are grouped by clustering TitaNet embeddings, with a time-smoothing pass so the speaker doesn't flip mid-sentence. Speaker changes then snap to the nearest sentence end. TitaNet was chosen after testing on a real team call: it separated speakers far better than CAM++ or ResNet34. Voiceprints are compared as-is, so a voice saved from one call matches the same person in the next.
- **Live mode.** On a Mac, audio comes from a small Swift helper ([capture/](capture/)): ScreenCaptureKit for system audio and AVAudioEngine for the mic. On Windows and Linux it comes from [tadween/capture.py](tadween/capture.py), using the soundcard package: a loopback of the speakers (WASAPI) on Windows, the speakers' monitor (PulseAudio or PipeWire) on Linux. Snippets go to a resident `whisper-server`. Whisper takes as long for a short snippet as for 30 seconds of audio, so when a computer without a GPU falls behind, the snippets waiting are sent together and it catches up.
- **Neural Engine.** On a Mac, `setup.sh` builds whisper.cpp with Core ML ([whisper/build.sh](whisper/build.sh)), so Whisper's encoder runs on the Apple Neural Engine instead of the GPU. Without it, Tadween uses Homebrew's whisper-cpp.
- **Windows and Linux.** Setup downloads (Windows) or builds (Linux) whisper.cpp for the computer: with CUDA when there is an NVIDIA GPU, otherwise for the CPU.

Speed on an M1 MacBook Air (8 GB), measured on a real team call:

| | GPU only | With the Neural Engine |
|---|---|---|
| Whisper, 8.4 min of speech | 58 s | 33 s, same text and timings |
| Whole pipeline, 10-minute clip | 71 s | 43 s |
| A live line appears after the speaker pauses | 3.6 s | 1.4 s |

*Settings → Transcription speed → Faster* switches to greedy decoding: about a quarter quicker again (26 s instead of 33 s for the Whisper step above). It drops most filler words ("um", "uh") and may word a few phrases differently.

## Setup

### macOS 13+ (Apple Silicon recommended)

```bash
git clone https://github.com/auqid/tadween.git
cd tadween
./setup.sh        # Homebrew ffmpeg + whisper.cpp, Python packages, ~1 GB of models, the capture helper,
                  # and the Neural Engine build of whisper.cpp (1.2 GB more, a few minutes once)
./tadween.sh      # opens http://127.0.0.1:8765
```

You need [Homebrew](https://brew.sh) and the Xcode Command Line Tools (`xcode-select --install`). The Neural Engine build also needs `cmake`, which setup installs with Homebrew. macOS prepares the Neural Engine model the first time each program uses it (about two minutes); setup does that for you, and after a macOS update the first transcription or live call may take that long to start.

### Windows 10 or 11 (64-bit Intel or AMD)

Install Python 3.10 or newer and Git, then open a new PowerShell window:

```powershell
winget install Python.Python.3.12
winget install Git.Git
```

```powershell
git clone https://github.com/auqid/tadween.git
cd tadween
powershell -ExecutionPolicy Bypass -File setup.ps1   # Python packages, ffmpeg, whisper.cpp, ~1 GB of models
.\tadween.cmd                                        # opens http://127.0.0.1:8765
```

Setup downloads a ready-made whisper.cpp: the CUDA build when it finds an NVIDIA GPU, otherwise the CPU build. It also fetches ffmpeg into `tools\` unless ffmpeg is already installed. Running it again skips whatever is already done. You can also start Tadween by double-clicking `tadween.cmd`.

### Linux (tested on Ubuntu 24.04)

```bash
sudo apt install python3 python3-venv ffmpeg git cmake g++ curl libpulse0   # Debian/Ubuntu; setup.sh names the Fedora and Arch packages
git clone https://github.com/auqid/tadween.git
cd tadween
./setup.sh        # Python packages, ~1 GB of models, and whisper.cpp built for this computer (a few minutes)
./tadween.sh      # opens http://127.0.0.1:8765
```

Any distribution with Python 3.10 or newer should work. Setup builds whisper.cpp with CUDA when an NVIDIA GPU and the CUDA toolkit are installed (both `nvidia-smi` and `nvcc` work), otherwise for the CPU. Live calls need PulseAudio, or PipeWire with `pipewire-pulse`, the default on current Ubuntu and Fedora.

### Live calls

Headphones give the cleanest result. Without them, Tadween drops mic lines that are just an echo of the call.

- **macOS.** The first time you start a live session, macOS asks for **Microphone** and **Screen & System Audio Recording** access. Both go to the app that launched Tadween, such as Terminal or VS Code. Allow both, then quit and reopen that app.
- **Windows.** The call audio is recorded from the default speakers or headset, so play the call through the default output device (*Settings → System → Sound*). For your mic, *Settings → Privacy & security → Microphone* must let desktop apps use it.
- **Linux.** The call audio is recorded from the default output device, and your voice from the default input. Choose both in your sound settings before you start.

### Models

The models are not in this repository (about 2 GB in all). Setup (`./setup.sh`, or `setup.ps1` on Windows) downloads them into `models/`; on a Mac, its last step (`./whisper/build.sh`) adds the Neural Engine encoder. Both skip files that are already there, so if a download fails, just run setup again.

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

On Windows, in PowerShell:

```powershell
mkdir models; cd models
curl.exe -LO https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q8_0.bin
curl.exe -LO https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx
curl.exe -LO https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_large.onnx
```

Keep the file names: Tadween looks for exactly these, and whisper.cpp finds the encoder by its name next to the Whisper model. The encoder is used only on a Mac, by the Core ML build of whisper.cpp, which `./whisper/build.sh` makes; it skips the download when the encoder is already in `models/`. (The release tag really is spelled "recongition".)

### Running faster

**On a Mac.** The numbers above come from a base M1 with 8 GB. On a newer or bigger Mac, in order of impact:

1. **Use the Neural Engine build.** `./setup.sh` installs it, and *Settings → Neural Engine* should say *On*. Newer chips have much faster Neural Engines, so this gains even more there.
2. **On a Pro, Max or Ultra chip, try the GPU too.** Those chips have many more GPU cores, and Whisper on the GPU may beat the Neural Engine. Time the same recording both ways, with the same *Transcription speed* setting, and keep whichever is faster:

   ```bash
   time ./tadween.sh transcribe "Team sync.m4a"                           # Neural Engine
   time TADWEEN_NEURAL_ENGINE=0 ./tadween.sh transcribe "Team sync.m4a"   # GPU only
   ```

   To stay on the GPU, start Tadween with `TADWEEN_NEURAL_ENGINE=0 ./tadween.sh`, or delete `whisper/bin`. On the GPU, *Faster* also turns on flash attention, the GPU's biggest speed-up.
3. **Choose Faster transcription** in Settings: about a quarter quicker. It drops most filler words and may word a few phrases differently.
4. **Match CPU threads to your performance cores.** Voice recognition runs on the CPU while Whisper works. `sysctl -n hw.perflevel0.physicalcpu` shows how many you have (4 on an M1, more on Pro and Max chips). Enter that in *Settings → CPU threads*, then restart Tadween.
5. **Leave enough memory free.** Transcribing a long call takes about 2 GB. With 16 GB or more this never matters; on 8 GB, quit memory-hungry apps first, or macOS starts swapping and everything slows down.
6. **Keep it plugged in and cool.** Turn off Low Power Mode. A fanless MacBook Air slows down as it warms up during a long job; a MacBook Pro or desktop Mac keeps its speed.

Intel Macs have no Neural Engine, so setup skips that step and transcription is slower.

**On Windows or Linux**, in order of impact:

1. **Use an NVIDIA GPU if the computer has one.** Whisper runs many times faster on it than on the CPU. Update the NVIDIA driver first. On Windows, setup picks the CUDA build when the driver supports CUDA 11.8 or newer; if you set Tadween up before installing the driver, delete `whisper\bin` and run `setup.ps1` again. On Linux, also install the CUDA toolkit, then run `./whisper/build.sh` again. `whisper/bin/VERSION` says which build you have. AMD and Intel GPUs aren't used: Whisper runs on the CPU there.
2. **With an NVIDIA GPU, also choose Faster transcription** in Settings. On a CPU alone it makes no real difference: there, nearly all of Whisper's time goes to the part both settings share. Measured on an M1's CPU with the GPU switched off, 80 seconds of a call took 48 s on *Most accurate* and 50 s on *Faster*.
3. **Give Whisper your CPU cores.** Tadween starts with one thread per core, up to 8. With more cores than that, raise *Settings → CPU threads* (Task Manager → Performance → CPU shows *Cores*; on Linux, `lscpu`), then restart Tadween. Live calls use at most 4, to leave room for the meeting app.
4. **Plug in and pick the best-performance power mode.** On battery, laptops slow their CPUs down.
5. **Leave enough memory free.** Transcribing a long call takes about 2 GB; 16 GB of memory leaves plenty of room.

## Command line

```bash
./tadween.sh transcribe "Team sync.m4a"                # writes "Team sync - transcript.txt" next to it
./tadween.sh transcribe call.mp4 --speakers 5 --format md
```

On Windows, use `.\tadween.cmd` in place of `./tadween.sh`.

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
- **Live lines appear in bursts.** Each line shows up a moment after a person pauses, not word by word. Without a GPU (a Windows or Linux computer without an NVIDIA card), each line costs the CPU as much as 30 seconds of audio (about 15 s on an M1's CPU), so lines arrive later and several at a time. The transcript you get after Stop is complete either way.
- **Live mode assumes your mic is only you.** If the call audio stays silent (people in the room, or a call on another device), the live view labels everything your mic hears as you and says so. The final transcript then tells the voices on your mic apart, as for a recording.
- **Live text and labels are a draft.** After you press Stop, the whole call is transcribed again for the final version. With the Neural Engine this takes about a tenth of the call's length. On a CPU alone it takes much longer: an M1's CPU needs about 50 s for each 80 s of conversation.
- **Settings default to English.** For other languages, change the language in Settings.

## Project layout

```
tadween/      Python app: pipeline, speakers, word fixes, live sessions, local web server,
              and capture.py, which records live calls on Windows and Linux
web/          The interface (plain HTML/CSS/JS, no build step)
capture/      macOS helper (Swift) that streams system audio / microphone as 16 kHz PCM
whisper/      build.sh builds whisper.cpp for this computer: Core ML on a Mac, CUDA or CPU on Linux
setup.sh      One-time setup, macOS and Linux     tadween.sh    Launcher, macOS and Linux
setup.ps1     One-time setup, Windows             tadween.cmd   Launcher, Windows
```

## Ideas for later

- A browser extension that reads exact participant names from Google Meet or the Zoom web client.
- Import Zoom's "separate audio file per participant" recordings for perfect speaker labels.
- Meeting summaries and action items.
