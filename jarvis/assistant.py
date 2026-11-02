"""The always-on core. Listening never stops and never blocks: a listener thread hears you (even while he thinks
or speaks), and each request runs as a *turn* on its own thread that a newer request or an interruption can cancel.

    microphone ─▶ listener ─┬─ wake word / conversation / "is this for me?" ─▶ route ─┬─ instant command ─▶ speak
                            └─ you talk over him ─▶ stop speaking, cancel the turn    └─ AI turn (streamed) ─▶ speak
"""
import collections
import logging
import queue
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

from google import genai
from google.genai import types

from . import learn, reflex
from .addresser import (NEG_FEEDBACK, POS_FEEDBACK, SLEEP, Addresser, Context, echo_overlap, judge, normalize,
                        strip_address, tokens)
from .agents import AgentManager, ApprovalBroker
from .audio import Microphone, Segmenter, WakeWord
from .brain import Brain, sentences
from .history import HistoryWatcher
from .memory import Memory
from .notifications import NotificationWatcher
from .social import SocialWatcher
from .speech import Speaker, Transcriber
from .tools import ctx, get_tools
from .util import DATA_DIR, audio_playing, frontmost_app, notify, system_busy
from .webinbox import WebInbox

log = logging.getLogger("jarvis")

SPECIAL_STATES = {"starting", "paused", "error", "blocked"}
DISMISS = {"thats all", "that's all", "thank you", "thanks", "goodbye", "bye", "dismissed", "no thanks", "im done", "i'm done"}
STOP = re.compile(r"^(stop|cancel|never mind|nevermind|forget it|hold on|wait|enough|that is enough|thats enough)( that| it| everything| talking| please)?$")
YES = {"yes", "yeah", "yep", "sure", "ok", "okay", "confirm", "proceed", "affirmative", "go", "approve", "approved"}
NO = {"no", "nope", "dont", "don't", "cancel", "stop", "negative", "not", "deny", "denied"}


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


class Turn:
    """One request being answered. Cancelling it stops the thinking, the tools and the speech."""

    def __init__(self, tid: int, text: str, auto: bool):
        self.id, self.text, self.auto = tid, text, auto
        self.cancel = threading.Event()
        self.spoke = False            # has he started talking yet?
        self.done = False
        self.interrupted = False
        self.started = time.time()


class Jarvis:
    def __init__(self, cfg, text_mode: bool = False):
        self.cfg = cfg
        self.client = make_client(cfg, fast=False)     # agents and tools: patient
        self.fast = make_client(cfg, fast=True)        # conversation and transcription: impatient
        self.memory = Memory(DATA_DIR / "jarvis.db")
        self.mic = None if text_mode else Microphone(cfg.audio.device)
        self.speaker = Speaker(cfg, self.client, self.mic, mute=text_mode)
        self.stt = Transcriber(cfg, self.fast)

        self.text_mode = text_mode
        self._override = "starting"
        self.paused = False       # set by the menu bar: releases the microphone entirely
        self.error = ""
        self._mic_retry = 0.0
        self.announce_q: "queue.Queue[str]" = queue.Queue()
        # conversation state
        self._turn = None
        self._turn_lock = threading.Lock()
        self._turn_n = 0
        self.follow_until = 0.0           # until then, whatever you say is for him (a conversation is going on)
        self._listen_window = 0.0         # after "Hey Jarvis" with nothing else: he is waiting for you
        self._user_talking = False
        self._confirm_future = None       # a yes/no question is waiting for your answer
        self._learn_busy = False
        self._announcing = False
        # hands-free listening (no wake word): see addresser.py
        self.addresser = Addresser(float(cfg.always_listen.sensitivity))
        self.auto_active = False
        self.asleep_until = 0.0           # "be quiet": only his name or a clear summons wakes him before this
        self.last_response_end = 0.0
        self._ignored = collections.deque(maxlen=5)   # recent sentences judged "not for me" (RAM only, 60 s)
        self._speech_log = collections.deque()        # ambient speech seconds, to notice a TV / call / podcast
        self._busy_until = 0.0
        self._auto_text = None
        self._seg = None
        self._people = (0.0, [])
        self._segments = ThreadPoolExecutor(max_workers=1, thread_name_prefix="segment")   # in order, off the listener
        self._stt_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="stt")
        self.broker = ApprovalBroker(cfg, self._announce)
        self.manager = AgentManager(cfg, self.client, self.memory, self._announce)

        ctx.cfg, ctx.client, ctx.speaker, ctx.memory = cfg, self.client, self.speaker, self.memory
        self.social = SocialWatcher(cfg, self._announce)
        self.notifier = NotificationWatcher(cfg, self._announce)
        self.history_watcher = HistoryWatcher(cfg, self._announce)
        self.webinbox = WebInbox(cfg, self.fast, self.notifier)
        ctx.manager, ctx.broker, ctx.announce, ctx.social = self.manager, self.broker, self._announce, self.social
        ctx.notifier, ctx.webinbox = self.notifier, self.webinbox
        ctx.confirm = self._confirm_text if text_mode else self._confirm_voice
        self.brain = Brain(cfg, self.fast, self.memory, get_tools(cfg), self.manager)

    # ── what he is doing right now (derived, so it can never get stuck) ──
    @property
    def state(self) -> str:
        ov = getattr(self, "_override", None)
        if ov:
            return ov
        sp = getattr(self, "speaker", None)
        if sp is not None and getattr(sp, "playing", False):
            return "speaking"
        if getattr(self, "_user_talking", False):
            return "recording"
        t = getattr(self, "_turn", None)
        if t is not None and not t.done:
            return "thinking"
        if time.time() < getattr(self, "_listen_window", 0.0):
            return "recording"
        return "listening"

    @state.setter
    def state(self, value: str) -> None:
        self._override = value if value in SPECIAL_STATES else None

    def jobs(self):
        """Everything in progress, for the on-screen timer: [{'who', 'elapsed', 'detail'}]."""
        now = time.time()
        out = [{"who": f"{agent} #{tid}", "elapsed": el, "detail": detail} for tid, agent, el, detail in self.manager.jobs()]
        t = self._turn
        if t is not None and not t.done and not t.spoke and now - t.started > 1.5:
            out.insert(0, {"who": "Jarvis", "elapsed": now - t.started, "detail": "thinking"})
        return out

    # ── messages from background agents ──────────────────────────
    def _announce(self, text: str) -> None:
        """Agents call this from their threads. Voice mode waits for a quiet moment; text mode prints."""
        if self.text_mode:
            self.speaker.say(text)
        else:
            self.announce_q.put(text)

    def _flush_announcements(self) -> bool:
        """Speak queued messages from agents in the background (so you can still interrupt him), but never over an answer."""
        if (self._turn is not None and not self._turn.done) or getattr(self, "_announcing", False) or self.announce_q.empty():
            return False
        self._announcing = True

        def work():
            try:
                while True:
                    self._say(self.announce_q.get_nowait())
            except queue.Empty:
                pass
            finally:
                self._announcing = False

        threading.Thread(target=work, daemon=True, name="announce").start()
        return False

    # ── yes/no questions (risky tools) ───────────────────────────
    def _confirm_voice(self, prompt: str) -> bool:
        fut: Future = Future()
        self._confirm_future = fut
        try:
            self.speaker.say(prompt + " Say yes or no.")
            self._listen_window = time.time() + 15
            return bool(fut.result(timeout=15))      # the listener fills this in from your next sentence
        except Exception:
            return False
        finally:
            self._confirm_future = None

    def _confirm_text(self, prompt: str) -> bool:
        return input(f"{prompt} [y/N] ").strip().lower().startswith("y")

    # ── speaking ─────────────────────────────────────────────────
    def _say(self, text: str) -> None:
        try:
            self.speaker.say(text)
        finally:
            self.last_response_end = time.time()

    def voice_level(self) -> float:
        """Loudness 0..1 of whoever is talking, for the halo: you while recording, Jarvis while speaking."""
        st = self.state
        if st == "speaking":
            return self.speaker.level()
        if st == "recording" and self.mic is not None:
            return self.mic.voice_level()
        return 0.0

    # ── microphone housekeeping ──────────────────────────────────
    def _idle(self) -> bool:
        """Runs between audio frames. True = skip this frame (paused, or an announcement was just spoken)."""
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
        if self.mic.blocked:                       # macOS gives silence instead of sound: be honest about it
            if self.state != "blocked":
                self.state, self.error = "blocked", "macOS is blocking the microphone"
                log.error("Microphone blocked. Allow it in System Settings > Privacy & Security > Microphone.")
                notify("Jarvis can't hear you", "Microphone access is blocked. Open the Jarvis menu > Fix microphone access.")
                from .permissions import mic_status, request_mic_access
                if mic_status() == "not_determined":
                    request_mic_access()           # pops the macOS 'allow microphone?' prompt
            if time.time() - self._mic_retry > 15:  # keep trying: it heals itself the moment access is granted
                self._mic_retry = time.time()
                self.mic.close()
                self.mic.open()
        elif self.state == "blocked":
            self.state, self.error = "listening", ""
            log.info("Microphone is working now")
        return self._flush_announcements()

    def _auto_enabled(self) -> bool:
        v = str(self.cfg.always_listen.enabled).lower()
        if v == "false":
            return False
        if not self.stt.whisper_available():       # never stream background speech to the cloud for this
            if v == "true":
                log.warning("Hands-free listening needs on-device speech recognition: pip install faster-whisper")
            return False
        return True

    # ── the listener: never blocks on thinking or speaking ───────
    def run(self) -> None:
        self.social.start()                       # message updates run alongside, in their own threads
        self.notifier.start(force=self.webinbox.enabled)   # also runs the shared announcer for the web inbox
        self.webinbox.start()
        self.history_watcher.start()
        threading.Thread(target=self._warm_up, daemon=True, name="warm-up").start()
        wake = None
        while True:
            try:
                if wake is None:
                    wake = WakeWord(self.cfg.wake)
                self.mic.open()
                self.error, self.state = "", "listening"
                self.auto_active = self._auto_enabled()
                log.info("Listening: wake word (%s)%s", self.cfg.wake.model,
                         " + hands-free (understands when you talk to him)" if self.auto_active else "")
                seg = Segmenter(self.mic, self.cfg.audio, wake, self.cfg.wake.threshold,
                                end_silence=float(self.cfg.always_listen.end_silence), playback=self.speaker,
                                barge_in=bool(self.cfg.interrupt.enabled))
                self._seg = seg
                while True:
                    wav, woke, early = seg.next(self._idle, on_wake=self._on_wake, on_pause=self._start_stt,
                                                on_start=self._on_speech_start, on_end=self._on_speech_end, on_barge=self._on_barge)
                    t_end = time.time()
                    self._segments.submit(self._process_segment, wav, woke, early, t_end)   # hear the next sentence meanwhile
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log.exception("Audio loop crashed; retrying in 5 s")
                self.error, self.state = str(exc)[:100], "error"
                time.sleep(5)

    def _conv_active(self) -> bool:
        t = self._turn
        return (t is not None and not t.done) or time.time() < self.follow_until or self._confirm_future is not None

    def _on_wake(self) -> None:
        self._user_talking = True                  # the halo appears the instant he hears his name

    def _on_speech_start(self, woke: bool) -> None:
        self._user_talking = bool(woke) or self._conv_active()   # no halo for speech that isn't for him

    def _on_speech_end(self) -> None:
        self._user_talking = False

    def _on_barge(self) -> None:
        """You talked over him (or said his name while he was speaking): stop at once."""
        self.follow_until = time.time() + max(8.0, float(self.cfg.audio.follow_up_seconds))
        if self._confirm_future is None:           # (an answer to his yes/no question must not cancel the question's own turn)
            t = self._turn
            if t is not None and not t.done:
                t.cancel.set()
                t.interrupted = True
        self.speaker.stop()
        log.info("Interrupted: he stopped talking")

    # ── what a sentence means ────────────────────────────────────
    def _ctx(self) -> Context:
        now = time.time()
        front = frontmost_app() if self.cfg.always_listen.pause_in_meetings else ""
        meeting = bool(front) and any(m.lower() in front.lower() for m in (self.cfg.always_listen.meeting_apps or []))
        if now - self._people[0] > 60:             # first names you've told him about ("brother: Arjun"), to spot "hey Arjun"
            names = []
            for topic, detail in self.memory.all_facts(limit=60):
                if any(k in topic.lower() for k in ("brother", "sister", "friend", "mom", "dad", "mum", "wife", "husband", "partner", "colleague", "boss")):
                    names += [n.lower() for n in re.findall(r"\b[A-Z][a-z]{2,}\b", detail)]
            self._people = (now, names)
        return Context(since_response=(now - self.last_response_end) if self.last_response_end else 1e9, meeting=meeting,
                       media=audio_playing() and self.state not in ("speaking", "thinking"),
                       asleep=now < self.asleep_until, people=self._people[1])

    def _transcribe(self, wav, early) -> str:
        try:
            return early.result(timeout=10) if early is not None else self.stt.transcribe(wav)
        except Exception:
            return self.stt.transcribe(wav)

    def _start_stt(self, wav: bytes):
        """Begin transcribing while you are still pausing; used if you turn out to be finished."""
        return self._stt_pool.submit(self.stt.transcribe, wav)

    def _process_segment(self, wav, woke: bool, early, t_end: float) -> None:
        try:
            self._process_segment_inner(wav, woke, early, t_end)
        except Exception:
            log.exception("could not process a sentence")

    def _process_segment_inner(self, wav, woke: bool, early, t_end: float) -> None:
        now = time.time()
        if wav is None:                           # his name, then nothing yet: he waits for you
            self._listen_window = self.follow_until = now + float(self.cfg.audio.first_utterance_timeout)
            return
        fut = self._confirm_future
        if fut is not None and not fut.done():    # you are answering his yes/no question
            words = set(re.findall(r"[a-z']+", (self._transcribe(wav, early) or "").lower()))
            fut.set_result(bool(words & YES) and not (words & NO))
            return
        if woke or self._conv_active():           # for him by definition: his name, or a conversation is going on
            text = self._transcribe(wav, early)
            if not text or not tokens(text):
                return
            if not woke and echo_overlap(text, self.speaker.recent) >= 0.6:
                return                            # his own voice coming back through the mic
            auto = False
            if woke:
                cleaned = strip_address(text)
                log.info("You (his name): %s", text)
                if not cleaned:
                    self._listen_window = self.follow_until = now + float(self.cfg.audio.first_utterance_timeout)
                    return
                self.addresser.learn(cleaned, 1.0, weight=0.7)      # teaches him how you sound when you talk to him
                text = cleaned
            else:
                log.info("You: %s", text)
                correction = bool(NEG_FEEDBACK.search(normalize(text)) or SLEEP.search(normalize(text)))
                if self._auto_text and not correction:   # you carried on talking to him: he was right to answer
                    self.addresser.learn(self._auto_text, 1.0, weight=0.5)
                    self._auto_text = None
            self._route(text, t_end, auto)
            return
        if not self.auto_active or self.paused or now < self._busy_until:
            return                                # without his name and without hands-free there is nothing to do
        spoken = getattr(self._seg, "last_speech_s", 1.0)
        self._speech_log.append((now, spoken))
        while self._speech_log and now - self._speech_log[0][0] > 60:
            self._speech_log.popleft()
        if sum(s for _t, s in self._speech_log) > 36:         # a TV, a podcast or a long call: stop listening for a while
            self._busy_until = now + 120
            log.info("Lots of speech around: hands-free paused for 2 minutes (his name still works)")
            return
        if self.cfg.performance.pause_background_when_busy and system_busy(0.95):
            return                                           # the Mac is flat out: don't add work
        text = self.stt.transcribe_local(wav)                # on this Mac only; nothing leaves it
        if not text or not tokens(text):
            return
        ctx_ = self._ctx()
        if echo_overlap(text, self.speaker.recent) >= 0.6:
            return
        verdict, p, parts = self.addresser.decide(text, ctx_)
        if verdict == "maybe" and self.cfg.always_listen.use_ai_judge and self.fast is not None:
            recent = " | ".join(t for _r, t in self.memory.recent(2)) or "(none)"
            yes = judge(self.fast, self.cfg.gemini.model, text, recent)
            self.addresser.learn(text, 1.0 if yes else 0.0, weight=0.4, ctx=ctx_)
            verdict = "yes" if yes else "no"
        if verdict == "yes":
            log.info("Hands-free: that was for me (%.2f, %s)", p, ", ".join(sorted(parts)))
            self._auto_text = text
            self._route(text, t_end, True)
        else:                                                # not for him: keep it 60 s in memory only, never on disk
            self._ignored.append((now, text))
            self.addresser.learn(text, 0.0, weight=0.05, ctx=ctx_)

    # ── routing a request ────────────────────────────────────────
    def _interrupt_all(self) -> str:
        """'Stop' / 'never mind': cancel what he is saying or thinking, otherwise the newest running task."""
        t = self._turn
        busy = (t is not None and not t.done) or self.speaker.playing
        if busy:
            if t is not None:
                t.cancel.set()
                t.interrupted = True
            self.speaker.stop()
            self.follow_until = time.time() + 5          # the conversation ends shortly after a stop
            return "stopped"
        running = [j for j in self.manager.jobs()]
        if running:
            tid = max(j[0] for j in running)
            self.manager.cancel(tid)
            return f"Stopped task {tid}."
        return ""

    def _route(self, text: str, t_end: float, auto: bool) -> None:
        norm = normalize(text)
        neg, sleep = bool(NEG_FEEDBACK.search(norm)), bool(SLEEP.search(norm))
        if neg or sleep:
            if neg and self._auto_text:                      # "I wasn't talking to you": learn strongly
                self.addresser.learn(self._auto_text, 0.0, weight=2.0)
                log.info("Learned: that wasn't for me")
            self._auto_text = None
            self.follow_until = 0.0
            if sleep:
                self.asleep_until = time.time() + 60 * float(self.cfg.always_listen.sleep_minutes)
                threading.Thread(target=self._say, args=("Okay. Say my name if you need me.",), daemon=True).start()
            else:
                threading.Thread(target=self._say, args=("Sorry about that.",), daemon=True).start()
            return
        if POS_FEEDBACK.search(norm) and self._ignored and time.time() - self._ignored[-1][0] < 60:
            _t, earlier = self._ignored.pop()                # "I was talking to you": answer what he skipped, and learn
            self.addresser.learn(earlier, 1.0, weight=2.0)
            log.info("Learned: I should have answered that")
            text = earlier
        cleaned = strip_address(text) or text
        stop_norm = _norm(cleaned)
        if STOP.match(stop_norm):
            said = self._interrupt_all()
            if said and said != "stopped":
                threading.Thread(target=self._say, args=(said,), daemon=True).start()
            return
        if stop_norm in DISMISS:
            self.follow_until = 0.0
            threading.Thread(target=self._say, args=("Very well.",), daemon=True).start()
            return
        threading.Thread(target=self._quick_or_ai, args=(cleaned, t_end, auto), daemon=True, name="route").start()

    def _quick_or_ai(self, text: str, t_end: float, auto: bool) -> None:
        try:
            quick = reflex.handle(text, self)
        except Exception:
            log.exception("instant command failed")
            quick = None
        if quick is not None:                                # answered on the spot; any AI turn already thinking carries on
            log.info("Instant command, no AI call")
            self.speaker.speak_stream(iter([quick]),
                                      on_audio=lambda: log.info("⏱ heard->voice %.2fs (instant)", time.time() - t_end))
            self.last_response_end = time.time()
            self.follow_until = time.time() + float(self.cfg.audio.follow_up_seconds)
            return
        self._start_turn(text, t_end, auto)

    # ── a turn: think and speak, cancellable at any moment ───────
    def _start_turn(self, text: str, t_end: float, auto: bool) -> None:
        merge = ""
        with self._turn_lock:
            prev = self._turn
            if prev is not None and not prev.done:
                if not prev.spoke:
                    merge = prev.text                        # he hadn't answered yet: the model gets both messages
                prev.cancel.set()
                self.speaker.stop()
            self._turn_n += 1
            turn = Turn(self._turn_n, text, auto)
            self._turn = turn
        self.follow_until = max(self.follow_until, time.time() + 60)   # he is working on it: you're still in conversation
        threading.Thread(target=self._run_turn, args=(turn, t_end, merge), daemon=True, name=f"turn-{turn.id}").start()

    def _run_turn(self, turn: Turn, t_end: float, merge: str) -> None:
        t = {"first_text": None}
        spoke = threading.Event()
        prefix = (f'(Earlier the user asked: "{merge}" and you had not answered yet. Then they said:) ' if merge else "")

        def timed(it):
            for sentence in it:
                if t["first_text"] is None:
                    t["first_text"] = time.time()
                yield sentence

        def on_audio():
            spoke.set()
            turn.spoke = True
            now = time.time()
            first = t["first_text"] or now
            log.info("⏱ heard->voice %.1fs  (AI first words %.1fs, voice start %.1fs, %s)", now - t_end,
                     first - t_end, now - first, self.brain.last_provider or "ai")

        def ack():                                   # a short spoken cue if the answer is slow
            if not spoke.is_set() and not turn.cancel.is_set():
                self.speaker.play_ack()

        timer = None
        if self.cfg.tts.ack:
            timer = threading.Timer(float(self.cfg.tts.ack_after_seconds), ack)
            timer.daemon = True
            timer.start()
        try:
            self.speaker.speak_stream(timed(sentences(self.brain.ask_stream(turn.text, cancel=turn.cancel, prefix=prefix))),
                                      on_audio=on_audio, cancel=turn.cancel)
        except Exception:
            log.exception("turn failed")
        finally:
            if timer:
                timer.cancel()
            turn.done = True
            if not turn.cancel.is_set():
                self.last_response_end = time.time()
                self.follow_until = time.time() + float(self.cfg.audio.follow_up_seconds)
                self._maybe_learn()

    def _maybe_learn(self) -> None:
        if self.cfg.memory.auto_learn and self.fast is not None and not self._learn_busy \
                and not (self.cfg.performance.pause_background_when_busy and system_busy()):
            self._learn_busy = True
            threading.Thread(target=self._learn, daemon=True, name="learn").start()

    def _learn(self) -> None:
        try:
            n = learn.learn(self.fast, self.cfg.gemini.model, self.memory, int(self.cfg.memory.max_facts_per_conversation))
            if n:
                log.info("Saved %d new fact(s) about you", n)
        except Exception as exc:
            log.debug("learning skipped: %s", exc)
        finally:
            self._learn_busy = False

    def _warm_up(self) -> None:
        """Load on-device speech recognition, cache the spoken cues, and keep connections warm while idle."""
        self.stt.warm()
        self.speaker.warm()
        while True:
            time.sleep(45)
            if self.state in ("listening", "paused"):
                self.speaker.keepalive()

    # ── typing instead of talking ────────────────────────────────
    def ask_text(self, text: str) -> str:
        """Used by the menu bar's 'Type to Jarvis' box: same brain, no microphone."""
        quick = reflex.handle(text, self)
        reply = quick if quick is not None else self.brain.ask(text)
        self._say(reply)
        return reply

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
