"""Voice. Speaking is pipelined: the next sentence is synthesised while the current one plays, so Jarvis
starts talking as soon as the first sentence exists. Hearing uses Gemini (or ElevenLabs Scribe)."""
import logging
import queue
import subprocess
import threading

import numpy as np
import requests
import sounddevice as sd
from google.genai import types

from .util import clean_for_speech

log = logging.getLogger("jarvis.speech")


class Speaker:
    def __init__(self, cfg, client, mic=None, mute: bool = False):
        self.cfg = cfg.tts
        self.client = client
        self.mic = mic
        self.mute = mute
        self.el_key = cfg.elevenlabs_key
        self._http = requests.Session()        # keeps the HTTPS connection warm: saves ~200 ms per sentence
        self._lock = threading.Lock()          # timers, agents and Jarvis never talk over each other
        self._stop = threading.Event()
        self._proc = None

    # ── public ───────────────────────────────────────────────────
    def say(self, text: str) -> None:
        self.speak_stream(iter([text]))

    def speak_stream(self, sentences, on_audio=None) -> None:
        """Speak an iterable of sentences. on_audio() fires right before the first sound."""
        if self.mute:
            for s in sentences:
                s = clean_for_speech(s)
                if s:
                    log.info("Jarvis: %s", s)
                    print(f"Jarvis: {s}")
            return
        self._stop.clear()
        q: "queue.Queue" = queue.Queue(maxsize=3)

        def produce():
            try:
                for s in sentences:
                    if self._stop.is_set():
                        break
                    s = clean_for_speech(s)
                    if s:
                        log.info("Jarvis: %s", s)
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
            if self._stop.is_set():
                continue                        # drain quietly
            if first:
                first = False
                if on_audio:
                    on_audio()
            with self._lock:                    # held per sentence, so a tool can ask a question mid-answer
                self._play(item)
        if self.mic is not None:
            self.mic.flush()                    # don't hear ourselves

    def stop(self) -> None:
        """Cut off whatever Jarvis is saying."""
        self._stop.set()
        try:
            sd.stop()
        except Exception:
            pass
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    # ── synthesis ────────────────────────────────────────────────
    def _synth(self, text: str):
        engine = self.cfg.engine
        pcm = self._elevenlabs_pcm(text) if engine == "elevenlabs" else self._gemini_pcm(text) if engine == "gemini" else None
        return ("pcm", pcm) if pcm else ("say", text)   # the Mac voice is the always-works fallback

    def _elevenlabs_pcm(self, text: str):
        c = self.cfg
        if not self.el_key or len(text) > c.elevenlabs_max_chars:
            return None                          # no key, or a long sentence: use the free voice
        try:
            r = self._http.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{c.elevenlabs_voice_id}",
                params={"output_format": "pcm_24000"},
                headers={"xi-api-key": self.el_key, "Content-Type": "application/json"},
                json={"text": text, "model_id": c.elevenlabs_model}, timeout=15)
            if r.status_code != 200:
                log.warning("ElevenLabs error %s: %s", r.status_code, r.text[:160])
                return None
            return r.content
        except Exception as exc:
            log.warning("ElevenLabs failed (%s); using the Mac voice", str(exc)[:100])
            return None

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
    def _play(self, item) -> None:
        kind, payload = item
        if kind == "pcm":
            sd.play(np.frombuffer(payload[: len(payload) // 2 * 2], dtype=np.int16), 24000)
            sd.wait()
        else:
            self._proc = subprocess.Popen(
                ["say", "-v", self.cfg.mac_voice, "-r", str(self.cfg.mac_rate), "-f", "-"], stdin=subprocess.PIPE)
            self._proc.communicate(payload.encode("utf-8"))
            self._proc = None


class Transcriber:
    PROMPT = ("Transcribe the speech in this audio verbatim. Output only the transcript. "
              "If there is no intelligible speech, output exactly: [silence]")

    def __init__(self, cfg, client):
        self.model = cfg.gemini.stt_model
        self.client = client
        self.provider = cfg.stt.provider
        self.el_model = cfg.stt.elevenlabs_model
        self.el_key = cfg.elevenlabs_key

    def transcribe(self, wav_bytes: bytes) -> str:
        order = ["elevenlabs", "gemini"] if self.provider == "elevenlabs" else ["gemini", "elevenlabs"]
        for name in order:
            try:
                if name == "gemini" and self.client is not None:
                    return self._gemini(wav_bytes)
                if name == "elevenlabs" and self.el_key:
                    return self._elevenlabs(wav_bytes)
            except Exception as exc:
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
