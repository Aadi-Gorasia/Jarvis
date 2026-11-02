"""Text-to-speech (macOS `say` or Gemini TTS) and speech-to-text (Gemini)."""
import logging
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
        self.el_key = cfg.elevenlabs_key
        self.client = client
        self.mic = mic
        self.mute = mute
        self._lock = threading.Lock()  # timers and the main loop never talk over each other

    def say(self, text: str) -> None:
        text = clean_for_speech(text)
        if not text:
            return
        log.info("Jarvis: %s", text)
        if self.mute:
            print(f"Jarvis: {text}")
            return
        with self._lock:
            ok = False
            if self.cfg.engine == "elevenlabs":
                ok = self._elevenlabs(text)
            elif self.cfg.engine == "gemini":
                ok = self._gemini(text)
            if not ok:
                self._mac_say(text)
            if self.mic is not None:
                self.mic.flush()  # don't hear ourselves

    def _mac_say(self, text: str) -> None:
        subprocess.run(
            ["say", "-v", self.cfg.mac_voice, "-r", str(self.cfg.mac_rate), "-f", "-"],
            input=text.encode("utf-8"), check=False,
        )

    def _elevenlabs(self, text: str) -> bool:
        c = self.cfg
        if not self.el_key:
            log.warning("ELEVENLABS_API_KEY is not set; using the Mac voice")
            return False
        if len(text) > c.elevenlabs_max_chars:
            return False  # long reply: save credits, use the free voice
        played = False
        try:
            with requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{c.elevenlabs_voice_id}/stream",
                params={"output_format": "pcm_24000"},
                headers={"xi-api-key": self.el_key, "Content-Type": "application/json"},
                json={"text": text, "model_id": c.elevenlabs_model},
                stream=True, timeout=30,
            ) as r:
                if r.status_code != 200:
                    log.warning("ElevenLabs error %s: %s", r.status_code, r.text[:200])
                    return False
                leftover = b""
                with sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16") as out:
                    for chunk in r.iter_content(4096):  # play while it downloads
                        data = leftover + chunk
                        n = len(data) // 2 * 2
                        if n:
                            out.write(data[:n])
                            played = True
                        leftover = data[n:]
            return True
        except Exception as exc:
            log.warning("ElevenLabs TTS failed (%s)", exc)
            return played  # if some audio already played, don't repeat it in another voice

    def _gemini(self, text: str) -> bool:
        try:
            prompt = f"{self.cfg.gemini_style}: {text}" if self.cfg.gemini_style else text
            resp = self.client.models.generate_content(
                model=self.cfg.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.cfg.gemini_voice)
                        )
                    ),
                ),
            )
            pcm = resp.candidates[0].content.parts[0].inline_data.data
            sd.play(np.frombuffer(pcm, dtype=np.int16), 24000)
            sd.wait()
            return True
        except Exception as exc:
            log.warning("Gemini TTS failed (%s); falling back to macOS voice", exc)
            return False


class Transcriber:
    PROMPT = (
        "Transcribe the speech in this audio verbatim. Output only the transcript. "
        "If there is no intelligible speech, output exactly: [silence]"
    )

    def __init__(self, cfg, client):
        self.model = cfg.gemini.stt_model
        self.client = client
        self.provider = cfg.stt.provider
        self.el_model = cfg.stt.elevenlabs_model
        self.el_key = cfg.elevenlabs_key

    def transcribe(self, wav_bytes: bytes) -> str:
        if self.provider == "elevenlabs" and self.el_key:
            try:
                r = requests.post("https://api.elevenlabs.io/v1/speech-to-text",
                                  headers={"xi-api-key": self.el_key},
                                  data={"model_id": self.el_model, "tag_audio_events": "false"},
                                  files={"file": ("speech.wav", wav_bytes, "audio/wav")}, timeout=30)
                r.raise_for_status()
                return (r.json().get("text") or "").strip()
            except Exception as exc:
                log.warning("ElevenLabs STT failed (%s); using Gemini", exc)
        return self._gemini(wav_bytes)

    def _gemini(self, wav_bytes: bytes) -> str:
        try:
            resp = self.client.models.generate_content(
                model=self.model,
                contents=[types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav"), self.PROMPT],
            )
        except Exception:
            log.exception("transcription failed")
            return ""
        text = (resp.text or "").strip()
        return "" if (not text or "[silence]" in text.lower()) else text
