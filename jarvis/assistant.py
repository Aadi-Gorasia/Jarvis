"""The always-on loop: wake word -> record -> transcribe -> Gemini (+tools) -> speak -> follow-ups."""
import logging
import re
import time

from google import genai

from .audio import Microphone, Recorder, WakeWord, play_sound
from .brain import Brain
from .memory import Memory
from .speech import Speaker, Transcriber
from .tools import ctx, get_tools
from .util import DATA_DIR

log = logging.getLogger("jarvis")

DISMISS = {"thats all", "that's all", "thank you", "thanks", "never mind", "nevermind", "stop",
           "goodbye", "bye", "go to sleep", "dismissed", "no thanks", "im done", "i'm done"}
YES = {"yes", "yeah", "yep", "sure", "ok", "okay", "confirm", "proceed", "affirmative", "go"}
NO = {"no", "nope", "dont", "don't", "cancel", "stop", "negative", "not"}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z' ]", "", text.lower()).replace("jarvis", "").strip()


class Jarvis:
    def __init__(self, cfg, text_mode: bool = False):
        self.cfg = cfg
        self.client = genai.Client(api_key=cfg.api_key)
        self.memory = Memory(DATA_DIR / "jarvis.db")
        self.mic = None if text_mode else Microphone(cfg.audio.device)
        self.recorder = None if text_mode else Recorder(self.mic, cfg.audio)
        self.speaker = Speaker(cfg, self.client, self.mic, mute=text_mode)
        self.stt = Transcriber(cfg, self.client)

        ctx.cfg, ctx.client, ctx.speaker, ctx.memory = cfg, self.client, self.speaker, self.memory
        ctx.confirm = self._confirm_text if text_mode else self._confirm_voice
        self.brain = Brain(cfg, self.client, self.memory, get_tools(cfg))

    # ── confirmation for risky tools ─────────────────────────────
    def _confirm_voice(self, prompt: str) -> bool:
        self.speaker.say(prompt + " Say yes or no.")
        audio = self.recorder.record(start_timeout=6)
        if not audio:
            return False
        words = set(re.findall(r"[a-z']+", self.stt.transcribe(audio).lower()))
        return bool(words & YES) and not (words & NO)

    def _confirm_text(self, prompt: str) -> bool:
        return input(f"{prompt} [y/N] ").strip().lower().startswith("y")

    # ── always-on voice loop ─────────────────────────────────────
    def run(self) -> None:
        wake = WakeWord(self.cfg.wake)
        while True:
            try:
                self.mic.open()
                log.info("Listening for the wake word (%s)...", self.cfg.wake.model)
                while True:
                    wake.wait(self.mic)
                    self.session()
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Audio loop crashed; retrying in 5 s")
                time.sleep(5)

    def session(self) -> None:
        log.info("Wake word detected")
        if self.cfg.audio.wake_sound:
            play_sound("Tink")
            self.mic.flush()
        audio = self.recorder.record(self.cfg.audio.first_utterance_timeout)
        while audio:
            text = self.stt.transcribe(audio)
            if not text:
                break
            log.info("You: %s", text)
            if _norm(text) in DISMISS:
                self.speaker.say("Very well.")
                break
            self.speaker.say(self.brain.ask(text))
            audio = self.recorder.record(self.cfg.audio.follow_up_seconds)  # follow-up, no wake word needed
        log.info("Back to sleep")

    # ── keyboard mode for testing without a mic ──────────────────
    def run_text(self) -> None:
        print("Text mode. Type to talk to Jarvis, Ctrl-C to quit.")
        while True:
            try:
                text = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if text:
                self.speaker.say(self.brain.ask(text))
