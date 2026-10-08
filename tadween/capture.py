"""Live-call audio on Windows and Linux: the microphone, or what the speakers play (loopback).

    python -m tadween.capture mic|system

Same contract as the macOS helper (capture/TadweenCapture.swift): 16 kHz mono 16-bit PCM on stdout, logs on
stderr plus one READY line once capture runs, exit 0 when stopped and 1 on an error. Uses the soundcard package:
WASAPI on Windows (loopback of the default speakers), PulseAudio or PipeWire on Linux (the speakers' monitor).
"""
import sys

import numpy as np

RATE = 16000
BLOCK = RATE // 10  # 0.1 s per write, as the macOS helper does


def log(message):
    print(f"tadween-capture: {message}", file=sys.stderr, flush=True)


def device(source):
    try:
        import soundcard as sc
    except Exception as e:  # on Linux: no libpulse, or no sound server (soundcard says only "AssertionError")
        if not sys.platform.startswith("linux"):
            raise
        raise RuntimeError("Can't reach the sound system. Live calls need PulseAudio, or PipeWire with "
                           "pipewire-pulse (libpulse0 on Debian and Ubuntu).") from e
    if source == "mic":
        return sc.default_microphone()
    # What the call sounds like: a loopback of the default speakers. Windows gives it the speakers' id,
    # PulseAudio and PipeWire name it "<speakers' id>.monitor". Not by name: a laptop's microphone is often
    # called exactly what its speakers are ("Built-in Audio Analog Stereo").
    speakers = sc.default_speaker()
    loopbacks = {m.id: m for m in sc.all_microphones(include_loopback=True) if m.isloopback}
    for id in (speakers.id, f"{speakers.id}.monitor"):
        if id in loopbacks:
            return loopbacks[id]
    raise RuntimeError(f"no way to record what {speakers.name} plays")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("mic", "system"):
        print(__doc__, file=sys.stderr)
        sys.exit(64)
    source = sys.argv[1]
    try:
        dev = device(source)
        log(f"capturing {'the microphone' if source == 'mic' else 'system audio'} from {dev.name}")
        out = sys.stdout.buffer
        with dev.recorder(samplerate=RATE, blocksize=BLOCK) as rec:  # the system resamples to 16 kHz
            print("READY", file=sys.stderr, flush=True)
            while True:
                x = rec.record(numframes=BLOCK)
                if x.ndim > 1:
                    x = x.mean(axis=1)  # mix every channel down, so nothing on one side is lost
                out.write((np.clip(x, -1.0, 1.0) * 32767).astype("<i2").tobytes())
                out.flush()
    except (BrokenPipeError, KeyboardInterrupt):  # Tadween stopped reading: the call is over
        pass
    except Exception as e:
        log(str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
