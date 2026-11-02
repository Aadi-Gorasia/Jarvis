"""Microphone capture, local wake-word detection, and utterance recording."""
import collections
import io
import logging
import queue
import subprocess
import time
import wave

import numpy as np
import sounddevice as sd

from .util import DATA_DIR

log = logging.getLogger("jarvis.audio")

RATE = 16000
MIN_SPEECH_S = 0.5  # shortest burst that counts as a real sentence
FRAME = 1280  # 80 ms @ 16 kHz, the frame size openWakeWord expects


class Microphone:
    """One always-open input stream. Wake word and recorder both read from it."""

    def __init__(self, device=None):
        self.device = device
        self.q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=300)
        self.stream = None
        self._zero_run = 0
        self._warned = False

    def open(self):
        self.close()
        self.stream = sd.InputStream(
            samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME,
            device=self.device, callback=self._callback,
        )
        self.stream.start()
        self._zero_run = 0
        self._warned = False

    def _callback(self, indata, frames, time_info, status):
        try:
            self.q.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass

    def read(self, timeout: float = 3.0) -> np.ndarray:
        try:
            frame = self.q.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError("microphone stopped delivering audio")
        # A mic without permission delivers pure digital silence.
        if not frame.any():
            self._zero_run += 1
            if self._zero_run == 150 and not self._warned:  # ~12 s
                self._warned = True
                log.warning("Microphone is returning pure silence. Check macOS Privacy & Security > Microphone.")
        else:
            self._zero_run = 0
        return frame

    def flush(self):
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass

    def close(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            finally:
                self.stream = None


RELEASE_URL = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"


def _fetch(name: str, folder) -> "pathlib.Path":
    """Download one model file from the openWakeWord GitHub release (skipped if already there)."""
    import requests

    dest = folder / name
    if dest.exists() and dest.stat().st_size > 10_000:
        return dest
    folder.mkdir(parents=True, exist_ok=True)
    log.info("Downloading wake-word file %s ...", name)
    try:
        r = requests.get(RELEASE_URL + name, timeout=90, allow_redirects=True)
        r.raise_for_status()
    except Exception as exc:
        raise RuntimeError(f"Could not download {name} ({exc}). Download it by hand: curl -L -o '{dest}' {RELEASE_URL}{name}")
    if len(r.content) < 10_000:
        raise RuntimeError(f"Downloaded {name} looks wrong ({len(r.content)} bytes). Download it by hand: "
                           f"curl -L -o '{dest}' {RELEASE_URL}{name}")
    dest.write_bytes(r.content)
    return dest


def _ensure_models(model_name: str) -> str:
    """openWakeWord 0.6 no longer ships its pre-trained models, so fetch what we need ourselves."""
    import pathlib
    import openwakeword

    if model_name.endswith(".onnx") and pathlib.Path(model_name).expanduser().exists():
        wake_path = pathlib.Path(model_name).expanduser()      # a custom model you trained
    else:
        wake_path = _fetch(f"{model_name}_v0.1.onnx", DATA_DIR / "models")
    feature_dir = pathlib.Path(openwakeword.__file__).parent / "resources" / "models"
    for name in ("melspectrogram.onnx", "embedding_model.onnx"):  # shared feature extractors
        _fetch(name, feature_dir)
    return str(wake_path)


class WakeWord:
    def __init__(self, wake_cfg):
        from openwakeword.model import Model

        self.model = Model(wakeword_models=[_ensure_models(wake_cfg.model)], inference_framework="onnx")
        self.threshold = wake_cfg.threshold

    def wait(self, mic: Microphone, on_idle=None) -> None:
        """Block until the wake word. on_idle() runs every frame; if it returns True (it spoke), reset."""
        if hasattr(self.model, "reset"):
            self.model.reset()
        mic.flush()
        while True:
            if on_idle is not None and on_idle():
                if hasattr(self.model, "reset"):
                    self.model.reset()
                continue
            scores = self.model.predict(mic.read())
            if scores and max(scores.values()) >= self.threshold:
                return


class Recorder:
    """Records one spoken utterance using a simple adaptive energy VAD."""

    def __init__(self, mic: Microphone, audio_cfg):
        self.mic = mic
        self.cfg = audio_cfg
        self.last_speech_s = 0.0

    def record(self, start_timeout: float):
        """Return WAV bytes, or None if the user never started speaking."""
        cfg = self.cfg
        frame_s = FRAME / RATE
        preroll = collections.deque(maxlen=4)
        frames, started = [], False
        quiet = speech_s = noise = 0.0
        t0 = time.monotonic()
        t_started = t0
        while True:
            frame = self.mic.read()
            rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
            loud = rms > max(cfg.min_rms, noise * cfg.noise_multiplier)
            now = time.monotonic()
            if not started:
                noise = rms if noise == 0 else 0.9 * noise + 0.1 * rms
                preroll.append(frame)
                if loud:
                    started, t_started, quiet = True, now, 0.0
                    frames.extend(preroll)
                    speech_s += frame_s
                elif now - t0 > start_timeout:
                    return None
            else:
                frames.append(frame)
                if loud:
                    quiet = 0.0
                    speech_s += frame_s
                else:
                    quiet += frame_s
                if quiet >= cfg.silence_seconds:
                    if speech_s < MIN_SPEECH_S:
                        # only the tail of the wake word (or a click) so far: the user is still
                        # pausing before the command, so start over and keep waiting
                        started, frames, speech_s, quiet = False, [], 0.0, 0.0
                        preroll.clear()
                        continue
                    break
                if now - t_started > cfg.max_seconds:
                    break
        self.last_speech_s = speech_s
        if speech_s < 0.25:
            return None
        return pcm_to_wav(np.concatenate(frames))


def pcm_to_wav(pcm: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.astype(np.int16).tobytes())
    return buf.getvalue()


def play_sound(name: str = "Tink") -> None:
    subprocess.run(["afplay", f"/System/Library/Sounds/{name}.aiff"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def mic_test(seconds: int = 8, device=None) -> None:
    """Print live loudness so you can pick a good audio.min_rms."""
    mic = Microphone(device)
    mic.open()
    print(f"Speak normally, then stay quiet. Listening for {seconds}s...\n")
    end, peak = time.time() + seconds, 0.0
    try:
        while time.time() < end:
            f = mic.read()
            rms = float(np.sqrt(np.mean(f.astype(np.float32) ** 2)))
            peak = max(peak, rms)
            print(f"\rRMS {rms:7.1f} |{'#' * int(min(rms / 40, 60)):<60}|", end="", flush=True)
    finally:
        mic.close()
    print(f"\n\nPeak RMS: {peak:.0f}")
    if peak == 0:
        print("Pure silence: macOS is blocking microphone access for this process.")
    print("Set audio.min_rms in config.yaml between your quiet-room level and your speaking level.")


def wake_test(cfg, seconds: int = 40) -> None:
    """Live view of what the wake-word model hears. Say 'Hey Jarvis' and watch the score."""
    mic = Microphone(cfg.audio.device)
    mic.open()
    ww = WakeWord(cfg.wake)
    print(f"Say 'Hey Jarvis' (threshold {ww.threshold}). Listening for {seconds}s...\n")
    end, best = time.time() + seconds, 0.0
    try:
        while time.time() < end:
            f = mic.read()
            scores = ww.model.predict(f)
            score = max(scores.values()) if scores else 0.0
            best = max(best, score)
            rms = float(np.sqrt(np.mean(f.astype(np.float32) ** 2)))
            flag = "  <-- WAKE WORD DETECTED" if score >= ww.threshold else ""
            print(f"\rscore {score:4.2f} (best {best:4.2f}) mic {rms:6.0f} |{'#' * int(score * 30):<30}|{flag}   ",
                  end="\n" if flag else "", flush=True)
    finally:
        mic.close()
    print(f"\n\nBest score: {best:.2f}. If it never reached the threshold, lower wake.threshold in config.yaml.")
