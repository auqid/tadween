"""Live transcription: your microphone and the call's audio, transcribed while people talk.

Your mic is always you. Everything else (the call) comes from system audio and is grouped by
voice as it arrives. When the call ends, the full voice analysis runs once more for final labels.
"""
import queue
import subprocess
import threading
import time
from datetime import datetime

import numpy as np

from . import asr, audio, config, events, pipeline, speakers, store, vad, vocab

SR = config.SAMPLE_RATE
_session = None
_lock = threading.Lock()



class Source(threading.Thread):
    """One audio stream (mic or call audio) cut into utterances by voice activity."""

    def __init__(self, session, name, cmd=None, simulate=None, speed=1.0):
        super().__init__(daemon=True, name=f"tadween-{name}")
        self.session, self.name, self.cmd, self.simulate, self.speed = session, name, cmd, simulate, speed
        self.vad = vad.StreamingVAD()
        self.samples = 0
        self.skipped = 0  # padding samples the VAD never saw: its times run this far behind the stream's
        self.proc = None
        self.ready = False
        self.error = None
        self.log_tail = ""
        self.speaking = False
        self.heard = False  # any speech yet? Silent call audio means the mic is hearing everyone
        self.raw = open(session.dir / f"{name}.pcm", "wb")

    def run(self):
        try:
            self._from_file() if self.simulate else self._from_helper()
        except Exception as e:
            self.error = str(e)
            events.emit("live_error", source=self.name, message=self.error)
        finally:
            for start, x in self.vad.flush():
                self.session.heard(self.name, start + self.skipped / SR, x)
            self.raw.close()

    def _from_helper(self):
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._read_logs, daemon=True).start()
        asked = time.monotonic()
        while chunk := self.proc.stdout.read(3200):  # 0.1 s of 16 kHz s16le
            # Arrival time is capture time only if we had to wait for it: audio already waiting in the
            # pipe means this reader fell behind, not that the capture skipped anything.
            waited = time.monotonic() - asked > 0.05
            self._feed(np.frombuffer(chunk[:len(chunk) // 2 * 2], "<i2").astype(np.float32) / 32768, clock=waited)
            asked = time.monotonic()
        code = self.proc.wait()
        if code != 0 and not self.session.stopping:
            raise RuntimeError(self.log_tail or f"capture stopped (exit code {code})")

    def _read_logs(self):
        lines = []
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if line == "READY":
                self.ready = True
                events.emit("live_status", source=self.name, ready=True)
            elif line:
                lines = (lines + [line.removeprefix("tadween-capture: ")])[-3:]
                self.log_tail = "\n".join(lines)

    def _from_file(self):
        """Replays a recording as if it were the call (for trying things out without a call)."""
        x = audio.read_wav(self.simulate)
        self.ready = True
        step = SR // 10
        for i in range(0, len(x), step):
            if self.session.stopping:
                break
            self._feed(x[i:i + step], clock=False)
            time.sleep(0.1 / self.speed)

    def _feed(self, x, clock):
        if clock:  # capture can skip silence: pad so this stream stays on the session clock
            behind = (time.monotonic() - self.session.t0) - (self.samples + len(x)) / SR
            if behind > 0.5:
                n = int(behind * SR)
                self._push(np.zeros(min(n, SR), np.float32))  # enough silence to end an utterance
                if n > SR:  # Silero over minutes of zeros would hold this reader up
                    self.raw.write(bytes(2 * (n - SR)))
                    self.samples += n - SR
                    self.skipped += n - SR
        self._push(x)

    def _push(self, x):
        self.raw.write(audio.to_pcm16(x))
        self.samples += len(x)
        for start, utterance in self.vad.accept(x):
            self.heard = True
            self.session.heard(self.name, start + self.skipped / SR, utterance)
        if self.vad.speaking() != self.speaking:
            self.speaking = not self.speaking
            events.emit("live_activity", source=self.name, speaking=self.speaking, heard=self.heard)


class LiveSession:
    def __init__(self, title, mic=True, system=True, apps="", simulate=None, speed=1.0):
        if not simulate and not config.CAPTURE_BIN.exists():
            raise RuntimeError("The capture helper isn't built yet. Run ./setup.sh (or capture/build.sh).")
        if not (mic or system or simulate):
            raise ValueError("Pick at least one audio source.")
        self.settings = config.load_settings()
        self.t = store.create(title, kind="live", status="live")
        self.id, self.dir = self.t["id"], store.folder(self.t["id"])
        self.voc, self.bank = vocab.Vocabulary(), speakers.VoiceBank()
        self.prompt = asr.build_prompt(self.settings["initial_prompt"], self.voc.prompt_terms() + self.bank.names())
        self.voices = speakers.OnlineVoices()
        self.lines, self.speakers = [], {}
        self.queue = queue.Queue()
        self.stopping = self.running = False
        self.stop_lock = threading.Lock()
        self.next_label = 1
        bin_ = str(config.CAPTURE_BIN)
        self.sources = []
        if simulate:
            self.sources.append(Source(self, "system", simulate=simulate, speed=speed))
        else:
            if mic:
                self.sources.append(Source(self, "mic", [bin_, "mic"]))
            if system:
                self.sources.append(Source(self, "system", [bin_, "system"] + (["--apps", apps] if apps else [])))
        if any(s.name == "mic" for s in self.sources):
            self.speakers["ME"] = {"label": "ME", "name": self.settings["my_name"], "manual": True}

    def start(self):
        pipeline.live_active.set()
        try:
            self.server = asr.WhisperServer(self.settings)
        except Exception:
            pipeline.live_active.clear()
            store.delete(self.id)
            raise
        self.t0 = time.monotonic()
        self.started = time.time()
        self.running = True
        for s in self.sources:
            s.start()
        self.worker = threading.Thread(target=self._work, daemon=True, name="tadween-live-asr")
        self.worker.start()
        events.emit("live_started", id=self.id)

    def heard(self, source, start, x):
        if len(x) >= 0.3 * SR:
            self.queue.put((source, start, x))

    def _work(self):
        while (item := self.queue.get()) is not None:
            if self.stopping:  # the final pass transcribes the whole call again: don't make Stop wait on a backlog
                continue
            try:
                self._transcribe(*item)
            except Exception as e:
                events.emit("live_error", source=item[0], message=str(e))

    def _new_label(self):
        label = f"S{self.next_label}"
        self.next_label += 1
        self.speakers[label] = {"label": label, "name": f"Speaker {label[1:]}", "manual": False}
        return label

    def _transcribe(self, source, start, x):
        context = " ".join(line["text"] for line in self.lines[-2:])[-200:]
        segs = self.server.transcribe(x, f"{self.prompt} {context}".strip())
        if not segs:
            return
        words = []
        for s in segs:
            for w in s["words"] or [{"w": " " + s["text"], "s": s["start"], "e": s["end"], "p": 1.0}]:
                words.append({**w, "s": round(start + w["s"], 2), "e": round(start + w["e"], 2)})
        line = {"start": round(start, 2), "end": round(start + len(x) / SR, 2), "source": source,
                "text": " ".join(s["text"] for s in segs).strip(), "words": words}
        if source == "mic":
            if self._echo_of_call(line):
                return
            line["speaker"] = "ME"
        else:
            self._drop_mic_echoes(line)
            seconds = len(x) / SR
            if seconds >= 0.8:
                line["speaker"] = self.voices.assign(speakers.Embedder.get()(x), seconds, self._new_label)
            else:  # too short to recognise a voice: most likely whoever spoke last on the call
                last = next((ln for ln in reversed(self.lines) if ln["source"] == "system"), None)
                line["speaker"] = last["speaker"] if last else self.voices.assign(
                    speakers.Embedder.get()(x), seconds, self._new_label)
            self._name_known_voices()
        line["text"], fired = self.voc.apply(line["text"])
        if fired:
            line["autofix"] = fired
        line["index"] = len(self.lines)
        self.lines.append(line)
        events.emit("live_line", id=self.id, line=_public(line), speakers=self.speakers)

    def _echo_of_call(self, line):
        """On speakers (not headphones) the mic also hears the call; skip what the call audio already has."""
        call_words = [w for other in self.lines[-10:] if other["source"] == "system" and not other.get("removed")
                      for w in other["words"]]
        return pipeline.echo_of(line["text"], line["start"], line["end"], call_words)

    def _drop_mic_echoes(self, line):
        for other in self.lines[-10:]:
            if (other["source"] == "mic" and not other.get("removed")
                    and pipeline.echo_of(other["text"], other["start"], other["end"], line["words"])):
                other["removed"] = True
                events.emit("live_remove", id=self.id, index=other["index"])

    def _name_known_voices(self):
        """Once a voice has enough speech, check it against people you've named before."""
        unnamed = {lab: self.voices.centroid(lab) for lab, g in self.voices.groups.items()
                   if g["talk"] >= 8 and not self.speakers[lab]["manual"] and not self.speakers[lab].get("person")}
        if not unnamed:
            return
        taken = {s.get("person") for s in self.speakers.values()}
        for lab, (pid, name, score) in self.bank.match(unnamed, self.settings["voice_match_threshold"]).items():
            if pid not in taken:
                self.speakers[lab].update(name=name, person=pid, score=score)
                events.emit("live_speakers", id=self.id, speakers=self.speakers)

    def rename(self, label, name, remember=True):
        s = self.speakers[label]
        s.update(name=name.strip(), manual=True, remember=remember and label != "ME")
        if label == "ME":
            config.save_settings({"my_name": name.strip()})
        events.emit("live_speakers", id=self.id, speakers=self.speakers)

    def status(self):
        return {"running": self.running, "id": self.id, "title": self.t["title"], "started": self.started,
                "stopping": self.stopping,
                "lines": [_public(line) for line in self.lines if not line.get("removed")],
                "speakers": self.speakers,
                "sources": [{"name": s.name, "ready": s.ready, "error": s.error, "speaking": s.speaking, "heard": s.heard}
                            for s in self.sources]}

    def stop(self):
        """End the call: save audio and lines, then queue the final voice analysis."""
        with self.stop_lock:  # a second Stop (another tab, Ctrl+C) waits for this one, then has nothing to do
            if not self.running:
                return
            self.stopping = True
            for s in self.sources:
                if s.proc and s.proc.poll() is None:
                    s.proc.terminate()
            for s in self.sources:
                s.join(timeout=15)
            self.queue.put(None)
            self.worker.join(timeout=300)
            self.server.close()
            self.running = False
            try:
                self._save()
            finally:
                pipeline.live_active.clear()
            events.emit("live_stopped", id=self.id)
            events.emit("transcripts")

    def _save(self):
        # The raw tracks become .wav files and a playable mix in the final job, so Stop returns at once.
        length = max(((self.dir / f"{s.name}.pcm").stat().st_size // 2 for s in self.sources
                      if (self.dir / f"{s.name}.pcm").exists()), default=0)
        lines = [line for line in self.lines if not line.get("removed")]
        config.write_json(self.dir / "segments.json", [
            {"start": ln["start"], "end": ln["end"], "text": ln["text"], "words": ln["words"],
             **({"fixed": "ME"} if ln["source"] == "mic" else {})} for ln in lines])
        store.update(self.id, status="queued", duration=round(length / SR, 2), speakers=self.speakers,
                     turns=[{**_public(ln), "id": i + 1} for i, ln in enumerate(lines)])
        pipeline.enqueue(self.id)


def _public(line):
    return {k: line[k] for k in ("index", "start", "end", "source", "speaker", "text", "autofix") if k in line}


def start(title=None, mic=True, system=True, apps="", simulate=None, speed=1.0):
    global _session
    with _lock:
        if _session and _session.running:
            raise RuntimeError("A live session is already running.")
        session = LiveSession(title or f"Call {datetime.now():%b %-d, %-I:%M %p}", mic, system, apps, simulate, speed)
        session.start()
        _session = session
        return session.status()


def stop():
    with _lock:
        session = _session
    if session:
        session.stop()
        return {"id": session.id}
    return {}


def rename(label, name, remember=True):
    if not (_session and _session.running):
        raise RuntimeError("No live session is running.")
    _session.rename(label, name, remember)
    return _session.speakers


def status():
    return _session.status() if _session else {"running": False}
