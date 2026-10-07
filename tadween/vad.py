"""Voice activity detection (Silero, via sherpa-onnx): where in the audio is someone talking."""
import numpy as np
import sherpa_onnx

from . import config

SR = config.SAMPLE_RATE
WINDOW = 512  # samples Silero looks at per step


def _config(min_silence, max_speech):
    c = sherpa_onnx.VadModelConfig()
    c.silero_vad.model = str(config.VAD_MODEL)
    c.silero_vad.threshold = 0.5
    c.silero_vad.min_silence_duration = min_silence
    c.silero_vad.min_speech_duration = 0.25
    c.silero_vad.max_speech_duration = max_speech
    c.sample_rate = SR
    return c


def speech_regions(x, pad=0.15, progress=None):
    """[(start, end)] seconds of speech in a whole recording, padded and merged."""
    vad = sherpa_onnx.VoiceActivityDetector(_config(0.3, 20.0), buffer_size_in_seconds=120)
    found = []

    def drain():
        while not vad.empty():
            seg = vad.front
            found.append((seg.start / SR, (seg.start + len(seg.samples)) / SR))
            vad.pop()

    step_report = SR * 60
    for i in range(0, len(x), WINDOW):
        vad.accept_waveform(x[i:i + WINDOW])
        drain()
        if progress and i % step_report < WINDOW:
            progress(i / max(len(x), 1))
    vad.flush()
    drain()

    total = len(x) / SR
    merged = []
    for a, b in found:
        a, b = max(0.0, a - pad), min(total, b + pad)
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(round(a, 3), round(b, 3)) for a, b in merged]


class StreamingVAD:
    """Feed audio as it arrives; get back finished utterances as (start_seconds, samples)."""

    def __init__(self, min_silence=0.5, max_speech=15.0):
        self.vad = sherpa_onnx.VoiceActivityDetector(_config(min_silence, max_speech), buffer_size_in_seconds=60)
        self.pending = np.zeros(0, np.float32)

    def accept(self, x):
        self.pending = np.concatenate([self.pending, x])
        usable = len(self.pending) // WINDOW * WINDOW
        for i in range(0, usable, WINDOW):
            self.vad.accept_waveform(self.pending[i:i + WINDOW])
        self.pending = self.pending[usable:]
        return self._drain()

    def speaking(self):
        return self.vad.is_speech_detected()

    def flush(self):
        self.vad.flush()
        return self._drain()

    def _drain(self):
        out = []
        while not self.vad.empty():
            seg = self.vad.front
            out.append((seg.start / SR, np.array(seg.samples, dtype=np.float32)))
            self.vad.pop()
        return out
