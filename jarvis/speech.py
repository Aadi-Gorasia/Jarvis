"""Text-to-speech (macOS `say` or Gemini TTS) and speech-to-text (Gemini)."""
import logging
import subprocess
import threading

import numpy as np
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
            ok = self._gemini(text) if self.cfg.engine == "gemini" else False
            if not ok:
                self._mac_say(text)
            if self.mic is not None:
                self.mic.flush()  # don't hear ourselves

    def _mac_say(self, text: str) -> None:
        subprocess.run(
            ["say", "-v", self.cfg.mac_voice, "-r", str(self.cfg.mac_rate), "-f", "-"],
            input=text.encode("utf-8"), check=False,
        )

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

    def transcribe(self, wav_bytes: bytes) -> str:
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
