"""Voice. Speaking is streamed and pipelined: sound starts as soon as the first bytes of the first sentence arrive,
and the next sentence is prepared while the current one plays. Hearing runs on-device with Whisper when it is
installed (no network round trip), otherwise on Gemini."""
import collections
import hashlib
import io
import logging
import math
import queue
import random
import subprocess
import threading
import time
import wave

import numpy as np
import requests
import sounddevice as sd
from google.genai import types

from .util import DATA_DIR, clean_for_speech

log = logging.getLogger("jarvis.speech")
ACKS = ["Mm-hm.", "On it.", "One moment.", "Let me check."]


class Speaker:
    def __init__(self, cfg, client, mic=None, mute: bool = False):
        self.cfg = cfg.tts
        self.client = client
        self.mic = mic
        self.mute = mute
        self.el_key = cfg.elevenlabs_key
        self._http = requests.Session()        # keeps the HTTPS connection warm
        self._lock = threading.Lock()          # timers, agents and Jarvis never talk over each other
        self._stop = threading.Event()
        self._proc = None
        self._lvl, self._lvl_t = 0.0, 0.0      # loudness of what is being played, for the halo
        self._say_until = 0.0
        self._ack_pcm = {}
        self._epoch = 0                       # bumped by stop(): every stream started earlier is cancelled
        self._in_play = 0
        self._last_audio = 0.0
        self.playback_rms = 0.0               # loudness (int16 scale) of what is coming out of the speakers right now
        self.recent = collections.deque(maxlen=8)   # what Jarvis just said, so he never mistakes his own voice for yours

    # ── public ───────────────────────────────────────────────────
    def say(self, text: str) -> None:
        self.speak_stream(iter([text]))

    @property
    def playing(self) -> bool:
        """True while sound is coming out of the speakers (plus a short tail)."""
        return self._in_play > 0 or time.time() - self._last_audio < 0.3

    def level(self) -> float:
        """How loud Jarvis is right now, 0..1 (drives the halo while he talks)."""
        now = time.time()
        if now < self._say_until:                           # Mac voice: no audio data, so fake a speech-like rhythm
            return 0.30 + 0.55 * abs(math.sin(now * 7.3)) * abs(math.sin(now * 3.1 + 1))
        return self._lvl if now - self._lvl_t < 0.12 else 0.0

    def speak_stream(self, sentences, on_audio=None, cancel=None) -> None:
        """Speak an iterable of sentences. on_audio() fires right before the first sound. Cancellable at any moment:
        by stop() (you talked over him) or by the cancel event (a newer request replaced this one)."""
        if self.mute:
            for s in sentences:
                s = clean_for_speech(s)
                if s:
                    log.info("Jarvis: %s", s)
                    print(f"Jarvis: {s}")
            return
        epoch = self._epoch

        def cancelled() -> bool:
            return self._epoch != epoch or (cancel is not None and cancel.is_set())

        q: "queue.Queue" = queue.Queue(maxsize=3)

        def produce():
            try:
                for s in sentences:
                    if cancelled():
                        break
                    s = clean_for_speech(s)
                    if s:
                        log.info("Jarvis: %s", s)
                        self.recent.append((time.time(), s))
                        q.put(self._synth(s))
            except Exception:
                log.exception("speech source failed")
            finally:
                q.put(None)

        threading.Thread(target=produce, daemon=True, name="speech-producer").start()
        first = True
        while True:
            item = q.get()
            if item is None:
                break
            if cancelled():
                self._discard(item)
                continue                        # drain quietly
            if first:
                first = False
                if on_audio:
                    on_audio()
            with self._lock:                    # held per sentence, so a tool can ask a question mid-answer
                self._play(item, cancelled)
        if self.mic is not None and not cancelled():
            self.mic.flush()                    # don't hear ourselves

    def stop(self) -> None:
        """Cut off whatever Jarvis is saying, instantly."""
        self._epoch += 1
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    # ── "one moment" acknowledgements: cover a slow answer with a short spoken cue ──
    def warm(self) -> None:
        """Pre-synthesise the cues (cached on disk, so credits are spent once) and open the ElevenLabs connection."""
        try:
            if self.cfg.engine == "elevenlabs" and self.el_key:
                for text in ACKS:
                    path = DATA_DIR / "acks" / (hashlib.md5(f"{self.cfg.elevenlabs_voice_id}{text}".encode()).hexdigest() + ".pcm")
                    if not path.exists():
                        pcm = self._elevenlabs_bytes(text)
                        if pcm:
                            path.parent.mkdir(exist_ok=True)
                            path.write_bytes(pcm)
                    if path.exists():
                        self._ack_pcm[text] = path.read_bytes()
        except Exception as exc:
            log.debug("ack warm-up failed: %s", exc)

    def keepalive(self) -> None:
        """Called every ~45 s while idle so the next answer doesn't pay for a fresh TLS handshake."""
        if self.el_key and self.cfg.engine == "elevenlabs":
            try:
                self._http.get("https://api.elevenlabs.io/v1/models", headers={"xi-api-key": self.el_key}, timeout=5)
            except Exception:
                pass

    def play_ack(self) -> None:
        text = random.choice(ACKS)
        pcm = self._ack_pcm.get(text)
        with self._lock:
            self._play(("pcm", pcm) if pcm else ("say", text))

    # ── synthesis ────────────────────────────────────────────────
    def _synth(self, text: str):
        engine = self.cfg.engine
        if engine == "elevenlabs":
            resp = self._elevenlabs_open(text)
            if resp is not None:
                return ("stream", resp)
        elif engine == "gemini":
            pcm = self._gemini_pcm(text)
            if pcm:
                return ("pcm", pcm)
        return ("say", text)                    # the Mac voice is the always-works fallback

    def _el_request(self, text: str, stream: bool):
        c = self.cfg
        if not self.el_key or len(text) > c.elevenlabs_max_chars:
            return None                          # no key, or a long sentence: use the free voice
        try:
            r = self._http.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{c.elevenlabs_voice_id}" + ("/stream" if stream else ""),
                params={"output_format": "pcm_24000"},
                headers={"xi-api-key": self.el_key, "Content-Type": "application/json"},
                json={"text": text, "model_id": c.elevenlabs_model}, timeout=15, stream=stream)
            if r.status_code != 200:
                log.warning("ElevenLabs error %s: %s", r.status_code, r.text[:160])
                r.close()
                return None
            return r
        except Exception as exc:
            log.warning("ElevenLabs failed (%s); using the Mac voice", str(exc)[:100])
            return None

    def _elevenlabs_open(self, text: str):
        return self._el_request(text, stream=True)

    def _elevenlabs_bytes(self, text: str):
        r = self._el_request(text, stream=False)
        return r.content if r is not None else None

    def _gemini_pcm(self, text: str):
        try:
            prompt = f"{self.cfg.gemini_style}: {text}" if self.cfg.gemini_style else text
            resp = self.client.models.generate_content(
                model=self.cfg.gemini_model, contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.cfg.gemini_voice)))))
            return resp.candidates[0].content.parts[0].inline_data.data
        except Exception as exc:
            log.warning("Gemini TTS failed (%s); using the Mac voice", str(exc)[:100])
            return None

    # ── playback ─────────────────────────────────────────────────
    def _discard(self, item) -> None:
        if item[0] == "stream":
            try:
                item[1].close()
            except Exception:
                pass

    def _set_level(self, arr: np.ndarray) -> None:
        rms = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2))) if len(arr) else 0.0
        self.playback_rms = rms
        self._lvl, self._lvl_t, self._last_audio = min(1.0, rms / 5500.0) ** 0.6, time.time(), time.time()

    def _play(self, item, cancelled=lambda: False) -> None:
        kind, payload = item
        self._in_play += 1
        try:
            if kind in ("stream", "pcm"):
                chunks = payload.iter_content(2048) if kind == "stream" else (payload[i:i + 4096] for i in range(0, len(payload), 4096))
                leftover = b""
                try:
                    with sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16") as out:
                        for chunk in chunks:                 # play while it downloads
                            if cancelled():
                                out.abort()                  # cut the sound immediately
                                break
                            data = leftover + chunk
                            n = len(data) // 2 * 2
                            if n:
                                self._set_level(np.frombuffer(data[:n], dtype=np.int16))
                                out.write(data[:n])
                            leftover = data[n:]
                finally:
                    if kind == "stream":
                        payload.close()
                    self._lvl = self.playback_rms = 0.0
            else:
                self._say_until = time.time() + 600
                self.playback_rms = 2500.0                   # the Mac voice has no audio data; assume a typical loudness
                try:
                    self._proc = subprocess.Popen(
                        ["say", "-v", self.cfg.mac_voice, "-r", str(self.cfg.mac_rate), "-f", "-"], stdin=subprocess.PIPE)
                    self._proc.communicate(payload.encode("utf-8"))
                finally:
                    self._proc = None
                    self._say_until = 0.0
                    self.playback_rms = 0.0
                    self._last_audio = time.time()
        finally:
            self._in_play -= 1


class Transcriber:
    PROMPT = ("Transcribe the speech in this audio verbatim. Output only the transcript. "
              "If there is no intelligible speech, output exactly: [silence]")

    def __init__(self, cfg, client):
        self.model = cfg.gemini.stt_model
        self.client = client
        self.provider = cfg.stt.provider                    # auto | whisper | gemini | elevenlabs
        self.el_model = cfg.stt.elevenlabs_model
        self.el_key = cfg.elevenlabs_key
        self.whisper_name = cfg.stt.whisper_model
        self.whisper_lang = cfg.stt.whisper_language or None
        self.cpu_threads = cfg.performance.max_threads
        self._whisper = None
        self._whisper_failed = False

    # ── on-device Whisper (optional: pip install faster-whisper) ──
    def whisper_available(self) -> bool:
        if self._whisper_failed:
            return False
        try:
            import faster_whisper  # noqa: F401
            return True
        except Exception:
            return False

    def warm(self) -> None:
        """Load the on-device model in the background at startup so the first sentence isn't slow."""
        if self.provider in ("auto", "whisper") and self.whisper_available():
            try:
                self._load_whisper()
                log.info("On-device speech recognition ready (%s)", self.whisper_name)
            except Exception as exc:
                self._whisper_failed = True
                log.warning("On-device Whisper unavailable (%s); using Gemini for transcription", str(exc)[:120])

    def _load_whisper(self):
        if self._whisper is None:
            from faster_whisper import WhisperModel
            self._whisper = WhisperModel(self.whisper_name, device="cpu", compute_type="int8", cpu_threads=int(self.cpu_threads))
        return self._whisper

    def _transcribe_whisper(self, wav_bytes: bytes) -> str:
        with wave.open(io.BytesIO(wav_bytes)) as w:
            audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = self._load_whisper().transcribe(audio, language=self.whisper_lang, beam_size=1, vad_filter=False,
                                                      condition_on_previous_text=False)
        return " ".join(seg.text.strip() for seg in segments).strip()

    def transcribe_local(self, wav_bytes: bytes):
        """On-device only: nothing is uploaded. Returns None if Whisper isn't available."""
        if not self.whisper_available():
            return None
        try:
            return self._transcribe_whisper(wav_bytes)
        except Exception as exc:
            self._whisper_failed = True
            log.warning("on-device transcription failed (%s)", str(exc)[:100])
            return None

    # ── public ───────────────────────────────────────────────────
    def transcribe(self, wav_bytes: bytes) -> str:
        order = {"whisper": ["whisper", "gemini", "elevenlabs"], "elevenlabs": ["elevenlabs", "gemini"],
                 "gemini": ["gemini", "elevenlabs"], "auto": ["whisper", "gemini", "elevenlabs"]}.get(self.provider, ["gemini"])
        for name in order:
            try:
                if name == "whisper" and self.whisper_available():
                    return self._transcribe_whisper(wav_bytes)
                if name == "gemini" and self.client is not None:
                    return self._gemini(wav_bytes)
                if name == "elevenlabs" and self.el_key:
                    return self._elevenlabs(wav_bytes)
            except Exception as exc:
                if name == "whisper":
                    self._whisper_failed = True
                log.warning("%s transcription failed (%s); trying the next one", name, str(exc)[:100])
        return ""

    def _gemini(self, wav_bytes: bytes) -> str:
        resp = self.client.models.generate_content(
            model=self.model,
            contents=[types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"), self.PROMPT])
        text = (resp.text or "").strip()
        return "" if (not text or "[silence]" in text.lower()) else text

    def _elevenlabs(self, wav_bytes: bytes) -> str:
        r = requests.post("https://api.elevenlabs.io/v1/speech-to-text", headers={"xi-api-key": self.el_key},
                          data={"model_id": self.el_model, "tag_audio_events": "false"},
                          files={"file": ("speech.wav", wav_bytes, "audio/wav")}, timeout=20)
        r.raise_for_status()
        return (r.json().get("text") or "").strip()
