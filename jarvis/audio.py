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
        self.blocked = False  # True when the mic only delivers digital silence (permission missing)
        self.rms = 0.0       # loudness of the most recent frame
        self.ambient = 0.0   # running estimate of the room's noise floor

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
            if self._zero_run == 25 and not self.blocked:   # ~2 s of exact digital silence = macOS is withholding the mic
                self.blocked = True
                log.warning("Microphone is returning pure silence: macOS is blocking access for this process.")
        else:
            self._zero_run = 0
            self.blocked = False
        rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
        self.rms = rms
        if self.ambient == 0.0 or rms < self.ambient:
            self.ambient = rms if self.ambient == 0.0 else 0.8 * self.ambient + 0.2 * rms  # drops fast
        else:
            self.ambient += 0.002 * (rms - self.ambient)                                   # rises slowly
        return frame

    def voice_level(self, full_scale: float = 2500.0) -> float:
        """How loud you are right now, 0..1 (relative to the room's noise), for the halo."""
        floor = self.ambient * 1.3
        x = (self.rms - floor) / max(1.0, full_scale - floor)
        return max(0.0, min(1.0, x)) ** 0.6

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


class Segmenter:
    """Cuts the always-open microphone into spoken sentences while also listening for the wake word, so Jarvis can
    answer to his name instantly *or* work out for himself whether a sentence was meant for him.

    It also lets you interrupt him: while he is speaking it knows how loud his own voice is, learns how much of it
    reaches the mic, and treats anything clearly louder than that as you talking over him."""

    def __init__(self, mic, audio_cfg, wake, wake_threshold: float, end_silence: float = 0.6, max_utterance: float = 15.0,
                 playback=None, barge_in: bool = True):
        self.mic, self.cfg, self.wake = mic, audio_cfg, wake
        self.threshold, self.end_silence, self.max_utterance = wake_threshold, end_silence, max_utterance
        self.playback, self.barge_in = playback, barge_in
        self.coupling = 0.35          # how much of his own voice reaches the mic (headphones ~0, laptop speakers high)
        self.last_speech_s = 0.0

    def _playing(self) -> bool:
        pb = self.playback
        return pb is not None and (pb.playing or time.time() - getattr(pb, "_last_audio", 0.0) < 0.4)

    def next(self, on_idle=None, on_wake=None, on_pause=None, on_start=None, on_end=None, on_barge=None):
        """Block until a sentence ends. Returns (wav or None, woke, handle): woke = the wake word fired during it;
        handle = a transcription already started for it (if on_pause was given and you really were done)."""
        cfg = self.cfg
        frame_s = FRAME / RATE
        preroll = collections.deque(maxlen=5)
        frames, started, woke, barged = [], False, False, False
        quiet = speech_s = noise = 0.0
        barge = 0
        handle, fired_at, t_started = None, -1.0, 0.0
        if self.wake is not None and hasattr(self.wake.model, "reset"):
            self.wake.model.reset()
        self.mic.flush()
        while True:
            if on_idle is not None and on_idle():            # paused, or he just spoke an announcement
                frames, started, woke, barged, quiet, speech_s, handle, barge = [], False, False, False, 0.0, 0.0, None, 0
                preroll.clear()
                if self.wake is not None and hasattr(self.wake.model, "reset"):
                    self.wake.model.reset()
                continue
            frame = self.mic.read()
            playing = self.barge_in and self._playing()
            if self.wake is not None and not woke:
                scores = self.wake.model.predict(frame)
                if scores and max(scores.values()) >= self.threshold:
                    woke = True
                    if on_wake:
                        on_wake()                            # the halo appears the instant he hears his name
                    if playing and on_barge and not barged:
                        barged = True
                        on_barge()                           # saying his name always interrupts him
            rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
            floor = max(noise, getattr(self.mic, "ambient", 0.0))
            thr = max(cfg.min_rms, floor * cfg.noise_multiplier)
            if playing:                                       # his own voice is in the mic: demand much more than that
                thr = max(thr, self.coupling * self.playback.playback_rms * 2.0 + floor * 2.0)
            loud = rms > thr
            now = time.monotonic()
            if playing and not loud and self.playback.playback_rms > 200:
                self.coupling = 0.97 * self.coupling + 0.03 * min(3.0, rms / self.playback.playback_rms)
            if not started:
                if not playing:
                    noise = rms if noise == 0 else 0.9 * noise + 0.1 * rms
                preroll.append(frame)
                if loud:
                    barge += 1
                    if not playing or barge >= 3:             # over his voice it must be sustained (about a quarter second)
                        started, t_started, quiet = True, now, 0.0
                        frames.extend(preroll)
                        speech_s += frame_s * (barge if playing else 1)
                        if playing and on_barge and not barged:
                            barged = True
                            on_barge()
                        if on_start:
                            on_start(woke)
                else:
                    barge = 0
                continue
            frames.append(frame)
            if loud:
                quiet = 0.0
                speech_s += frame_s
                handle = None
            else:
                quiet += frame_s
                if woke and on_pause and handle is None and quiet >= float(getattr(cfg, "speculative_after", 0.2)) and speech_s >= MIN_SPEECH_S:
                    handle, fired_at = on_pause(pcm_to_wav(np.concatenate(frames))), speech_s
            if quiet >= self.end_silence or now - t_started > self.max_utterance:
                if speech_s < MIN_SPEECH_S:                  # a click or cough: not a sentence
                    frames, started, quiet, speech_s, handle, barge = [], False, 0.0, 0.0, None, 0
                    preroll.clear()
                    if on_end:
                        on_end()
                    continue
                wav = pcm_to_wav(np.concatenate(frames))
                self.last_speech_s = speech_s
                if on_end:
                    on_end()
                return wav, woke, (handle if handle is not None and speech_s == fired_at else None)


class Recorder:
    """Records one spoken utterance using a simple adaptive energy VAD."""

    def __init__(self, mic: Microphone, audio_cfg):
        self.mic = mic
        self.cfg = audio_cfg
        self.last_speech_s = 0.0

    def record(self, start_timeout: float):
        """Return WAV bytes, or None if the user never started speaking."""
        return self.record_ex(start_timeout)[0]

    def record_ex(self, start_timeout: float, on_pause=None):
        """Like record(), returning (wav, handle). If on_pause is given it is called with the audio so far as soon as
        you pause briefly (before the sentence is officially over), and whatever it returns (for example a transcription
        already in progress) comes back as `handle` if you really were finished. That hides most of the wait."""
        cfg = self.cfg
        frame_s = FRAME / RATE
        spec_after = float(getattr(cfg, "speculative_after", 0.2))
        preroll = collections.deque(maxlen=4)
        frames, started = [], False
        quiet = speech_s = noise = 0.0
        handle, fired_at = None, -1.0
        t0 = time.monotonic()
        t_started = t0
        while True:
            frame = self.mic.read()
            rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
            floor = max(noise, getattr(self.mic, "ambient", 0.0))
            loud = rms > max(cfg.min_rms, floor * cfg.noise_multiplier)
            now = time.monotonic()
            if not started:
                noise = rms if noise == 0 else 0.9 * noise + 0.1 * rms
                preroll.append(frame)
                if loud:
                    started, t_started, quiet = True, now, 0.0
                    frames.extend(preroll)
                    speech_s += frame_s
                elif now - t0 > start_timeout:
                    return None, None
            else:
                frames.append(frame)
                if loud:
                    quiet = 0.0
                    speech_s += frame_s
                    handle = None                       # you kept talking: any early guess is stale
                else:
                    quiet += frame_s
                    if on_pause and handle is None and quiet >= spec_after and speech_s >= MIN_SPEECH_S:
                        handle, fired_at = on_pause(pcm_to_wav(np.concatenate(frames))), speech_s   # start early
                if quiet >= cfg.silence_seconds:
                    if speech_s < MIN_SPEECH_S:
                        # only the tail of the wake word (or a click) so far: the user is still
                        # pausing before the command, so start over and keep waiting
                        started, frames, speech_s, quiet = False, [], 0.0, 0.0
                        preroll.clear()
                        handle = None
                        continue
                    break
                if now - t_started > cfg.max_seconds:
                    break
        self.last_speech_s = speech_s
        if speech_s < 0.25:
            return None, None
        wav = pcm_to_wav(np.concatenate(frames))
        return wav, (handle if handle is not None and speech_s == fired_at else None)   # None if you kept talking after the pause


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
