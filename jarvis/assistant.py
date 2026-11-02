"""The always-on loop: wake word -> listen -> (instant command | AI, streamed) -> speak -> follow-ups."""
import logging
import queue
import re
import time

from google import genai
from google.genai import types

from . import reflex
from .agents import AgentManager, ApprovalBroker
from .audio import Microphone, Recorder, WakeWord, play_sound
from .brain import Brain, sentences
from .memory import Memory
from .speech import Speaker, Transcriber
from .tools import ctx, get_tools
from .util import DATA_DIR

log = logging.getLogger("jarvis")

DISMISS = {"thats all", "that's all", "thank you", "thanks", "never mind", "nevermind", "goodbye", "bye",
           "go to sleep", "dismissed", "no thanks", "im done", "i'm done"}
YES = {"yes", "yeah", "yep", "sure", "ok", "okay", "confirm", "proceed", "affirmative", "go"}
NO = {"no", "nope", "dont", "don't", "cancel", "stop", "negative", "not"}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z' ]", "", text.lower()).replace("jarvis", "").strip()


def make_client(cfg, fast: bool):
    """fast=True: short timeout and NO hidden SDK retries, so a busy model fails over in seconds instead of minutes."""
    if not cfg.api_key:
        return None
    if fast:
        try:
            return genai.Client(api_key=cfg.api_key, http_options=types.HttpOptions(
                timeout=int(float(cfg.gemini.timeout_seconds) * 1000),
                retry_options=types.HttpRetryOptions(attempts=1)))
        except Exception as exc:
            log.warning("could not set a fast timeout on the Gemini client (%s); using defaults", exc)
    return genai.Client(api_key=cfg.api_key)


class Jarvis:
    def __init__(self, cfg, text_mode: bool = False):
        self.cfg = cfg
        self.client = make_client(cfg, fast=False)     # agents and tools: patient
        self.fast = make_client(cfg, fast=True)        # conversation and transcription: impatient
        self.memory = Memory(DATA_DIR / "jarvis.db")
        self.mic = None if text_mode else Microphone(cfg.audio.device)
        self.recorder = None if text_mode else Recorder(self.mic, cfg.audio)
        self.speaker = Speaker(cfg, self.client, self.mic, mute=text_mode)
        self.stt = Transcriber(cfg, self.fast)

        self.text_mode = text_mode
        self.state = "starting"   # starting | listening | recording | thinking | speaking | paused | error
        self.paused = False       # set by the menu bar: releases the microphone entirely
        self.error = ""
        self.announce_q: "queue.Queue[str]" = queue.Queue()
        self.broker = ApprovalBroker(cfg, self._announce)
        self.manager = AgentManager(cfg, self.client, self.memory, self._announce)

        ctx.cfg, ctx.client, ctx.speaker, ctx.memory = cfg, self.client, self.speaker, self.memory
        ctx.manager, ctx.broker, ctx.announce = self.manager, self.broker, self._announce
        ctx.confirm = self._confirm_text if text_mode else self._confirm_voice
        self.brain = Brain(cfg, self.fast, self.memory, get_tools(cfg), self.manager)

    # ── messages from background agents ──────────────────────────
    def _announce(self, text: str) -> None:
        """Agents call this from their threads. Voice mode waits for a quiet moment; text mode prints."""
        if self.text_mode:
            self.speaker.say(text)
        else:
            self.announce_q.put(text)

    def _flush_announcements(self) -> bool:
        spoke = False
        while True:
            try:
                msg = self.announce_q.get_nowait()
            except queue.Empty:
                return spoke
            self._say(msg)
            spoke = True

    # ── confirmation for risky foreground tools ──────────────────
    def _confirm_voice(self, prompt: str) -> bool:
        self._say(prompt + " Say yes or no.")
        audio = self.recorder.record(start_timeout=8)
        if not audio:
            return False
        words = set(re.findall(r"[a-z']+", self.stt.transcribe(audio).lower()))
        return bool(words & YES) and not (words & NO)

    def _confirm_text(self, prompt: str) -> bool:
        return input(f"{prompt} [y/N] ").strip().lower().startswith("y")

    # ── speaking ─────────────────────────────────────────────────
    def _say(self, text: str) -> None:
        prev = self.state
        self.state = "speaking"
        try:
            self.speaker.say(text)
        finally:
            self.state = prev if prev != "speaking" else "listening"

    def _respond(self, text: str, t_heard: float) -> None:
        """Answer one request: instant command if possible, otherwise stream the AI's answer sentence by sentence."""
        quick = reflex.handle(text, self)
        if quick is not None:
            log.info("Instant command, no AI call")
            self.state = "speaking"
            self.speaker.speak_stream(iter([quick]),
                                      on_audio=lambda: log.info("⏱ heard->voice %.1fs (instant)", time.time() - t_heard))
            return
        self.state = "thinking"

        def on_audio():
            self.state = "speaking"
            log.info("⏱ heard->voice %.1fs (%s)", time.time() - t_heard, self.brain.last_provider or "ai")

        self.speaker.speak_stream(sentences(self.brain.ask_stream(text)), on_audio=on_audio)

    # ── always-on voice loop ─────────────────────────────────────
    def _idle(self) -> bool:
        """Runs between audio frames while waiting for the wake word. True = skip this frame."""
        if self.paused:
            if self.mic.stream is not None:
                self.mic.close()                       # pause really means mic off (orange dot goes away)
                log.info("Paused: microphone released")
            self.state = "paused"
            time.sleep(0.25)
            return True
        if self.mic.stream is None:
            self.mic.open()
            self.state = "listening"
            log.info("Resumed: microphone open")
        return self._flush_announcements()

    def run(self) -> None:
        wake = None
        while True:
            try:
                if wake is None:
                    wake = WakeWord(self.cfg.wake)
                self.mic.open()
                self.error, self.state = "", "listening"
                log.info("Listening for the wake word (%s)...", self.cfg.wake.model)
                while True:
                    wake.wait(self.mic, self._idle)
                    self.session()
                    self.state = "listening"
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log.exception("Audio loop crashed; retrying in 5 s")
                self.error, self.state = str(exc)[:100], "error"
                time.sleep(5)

    def session(self) -> None:
        log.info("Wake word detected")
        self.state = "recording"                  # the halo appears now: that is your cue to speak
        if self.cfg.audio.wake_sound:
            play_sound("Tink")
            self.mic.flush()
        audio = self._listen(self.cfg.audio.first_utterance_timeout)
        while audio:
            t0 = time.time()
            text = self.stt.transcribe(audio)
            t_heard = time.time()
            if not text:
                log.info("Transcript was empty (heard sound but no words)")
                break
            log.info("You: %s  (transcribed in %.1fs)", text, t_heard - t0)
            if _norm(text) in DISMISS:
                self._say("Very well.")
                break
            self._respond(text, t0)
            audio = self._listen(self.cfg.audio.follow_up_seconds)  # follow-up, no wake word needed
        log.info("Back to sleep")

    def _listen(self, timeout: float):
        self.state = "recording"
        audio = self.recorder.record(timeout)
        if audio is None:
            self.state = "listening"
            log.info("No speech heard (mic level below audio.min_rms? run ./jarvisctl.sh doctor)")
        else:
            self.state = "thinking"
            log.info("Heard %.1fs of speech", self.recorder.last_speech_s)
            (DATA_DIR / "last_utterance.wav").write_bytes(audio)  # play it to hear what Jarvis heard
        return audio

    def ask_text(self, text: str) -> str:
        """Used by the menu bar's 'Type to Jarvis' box: same brain, no microphone."""
        prev = self.state
        self.state = "thinking"
        try:
            quick = reflex.handle(text, self)
            reply = quick if quick is not None else self.brain.ask(text)
        finally:
            self.state = prev
        self._say(reply)
        return reply

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
                self.speaker.speak_stream(iter([reflex.handle(text, self) or self.brain.ask(text)]))
