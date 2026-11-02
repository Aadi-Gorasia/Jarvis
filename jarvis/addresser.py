"""Works out whether you are talking to Jarvis, so you don't need a wake word.

How it decides (all on your Mac, in microseconds):
  1. Hand-written knowledge gives a score on day one: his name, summons ("wake up", "where were we"), requests
     ("open...", "remind me...", "can you..."), questions, how recently he last spoke, how long you talked, whether
     you addressed someone else ("hey mum"), meetings, and background media.
  2. A tiny personal model sits on top and learns from you: every "Hey Jarvis ..." teaches it what you sound like
     when you are talking to him; corrections ("I wasn't talking to you", "Jarvis, I was talking to you") teach it
     strongly. It gets better the more you use it.
  Borderline sentences can optionally be checked by a fast AI model (only that one sentence is sent)."""
import json
import logging
import math
import re
import threading
import zlib
from dataclasses import dataclass, field

import numpy as np

from .util import DATA_DIR

log = logging.getLogger("jarvis.addresser")
DIM = 4096
PATH = DATA_DIR / "addresser.npz"

NAME = re.compile(r"\b(jarvis|jarvas|jarvus|jervis|jarves|jarviz|service)\b")   # includes common mishearings
NAME_STRICT = re.compile(r"\b(jarvis|jarvas|jarvus|jervis|jarves|jarviz)\b")
SUMMONS = [r"wake up", r"are you (there|awake|up|listening|ready|with me)", r"you there", r"i m back", r"i am back",
           r"where were we", r"where we left off", r"pick up where", r"let s (get started|continue|pick up|begin|get to work)",
           r"good (morning|afternoon|evening|night)", r"\bdaddy s (here|home|back)",
           r"\bi m home\b", r"anyone home", r"talk to me", r"you awake", r"rise and shine", r"time to work"]
STARTERS = set("""open launch start play pause stop resume skip next previous set turn mute unmute remind tell show give find search
look check summarize summarise read write create make build fix clean delete move copy send call text email schedule add remove list
explain translate calculate convert compare plan help take save run install update commit push pull download upload organize
organise sort cancel continue repeat undo redo go switch close quit restart shut lock increase decrease raise lower bring get put
let remember forget note draft edit rename count define spell wake""".split())
Q_STARTS = set("what whats when where who whos whose how why which".split())
GREET = re.compile(r"^(hello|hi|hey|yo)( there)?$")
POLITE = re.compile(r"^(please|can you|could you|would you|will you|would you mind|do you mind|i need you to|i want you to|go ahead and|just)\b")
REQUEST = re.compile(r"\b(can|could|would|will|do|did|are|were) you\b|\b(tell|show|give|help|remind|let|get|bring) me\b|\bfor me\b")
TOPICS = set("""weather time date timer alarm reminder volume music song playlist spotify youtube email mail message messages whatsapp
instagram calendar meeting schedule news score temperature traffic github repo repository commit files folder desktop downloads
mac laptop computer battery screenshot clipboard notes note agent agents task tasks spreadsheet excel document""".split())
FILLERS = set("um uh hmm hm ha haha heh yeah yes no nah okay ok oh ah mhm uhh umm huh wow cool nice right sure fine".split())
VOCATIVES = re.compile(r"^(hey |hi |yo |ok |okay |oh )?(mom|mum|mommy|mummy|dad|daddy|papa|bro|man|dude|babe|baby|honey|sis|buddy|guys|everyone|sir|madam|miss)\b")
NEG_FEEDBACK = re.compile(r"\b(not you|wasn t talking to you|was not talking to you|m not talking to you|am not talking to you|"
                          r"stop listening|shut up|be quiet|ignore (that|me)|my bad|not talking to you)\b")
SLEEP = re.compile(r"\b(go to sleep|stop listening|be quiet|shut up|quiet mode|sleep mode)\b")
POS_FEEDBACK = re.compile(r"\b(i was talking to you|i was asking you|i said|did you (hear|not hear) (me|that)|were you listening|answer me|you didn t answer)\b")


@dataclass
class Context:
    since_response: float = 1e9          # seconds since Jarvis last spoke
    meeting: bool = False                # a video-call app is in front
    media: bool = False                  # audio is playing (music, video)
    asleep: bool = False                 # told to be quiet: only his name or a clear summons wakes him
    people: list = field(default_factory=list)   # first names from memory, to spot "hey Arjun"


def tokens(text: str):
    return re.findall(r"[a-z0-9]+", text.lower().replace("'", " "))


def normalize(text: str) -> str:
    return " ".join(tokens(text))


def strip_address(text: str) -> str:
    """Remove 'hey Jarvis' / 'Jarvis,' from the front and ', Jarvis' from the end."""
    t = re.sub(r"^\s*(hey|hi|ok|okay|yo|oh)?[ ,]*(jarvis|jarvas|jarvus|jervis)\b[ ,.:!?-]*", "", text.strip(), flags=re.I)
    return re.sub(r"[ ,.]*\b(jarvis|jarvas|jarvus|jervis)\b[ .!?]*$", "", t, flags=re.I).strip()


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, x))))


def echo_overlap(text: str, recent) -> float:
    """How much of this sentence is just Jarvis's own voice coming back through the mic."""
    a = set(tokens(text))
    best = 0.0
    for _t, said in recent:
        b = set(tokens(said))
        if a and b:
            best = max(best, len(a & b) / len(a | b))
    return best


class Addresser:
    def __init__(self, sensitivity: float = 0.6):
        self.hi = 0.90 - 0.40 * float(sensitivity)       # act above this
        self.lo = self.hi - 0.25                         # below this: ignore; between: borderline
        self.w = np.zeros(DIM, dtype=np.float32)
        self.updates = 0
        self.lock = threading.Lock()
        self._load()

    # ── persistence ──────────────────────────────────────────────
    def _load(self) -> None:
        try:
            data = np.load(PATH)
            self.w, self.updates = data["w"].astype(np.float32), int(data["n"])
        except Exception:
            pass

    def save(self) -> None:
        try:
            np.savez(PATH, w=self.w, n=self.updates)
        except Exception:
            log.debug("could not save the addressing model", exc_info=True)

    # ── scoring ──────────────────────────────────────────────────
    def _idx(self, toks):
        grams = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
        return [zlib.crc32(g.encode()) % DIM for g in grams] or [0]

    def base_logit(self, text: str, ctx: Context):
        t = normalize(text)
        toks = t.split()
        parts, x = {}, -2.2
        if NAME_STRICT.search(t) or NAME.search(t) and len(toks) <= 4:
            parts["name"] = 6.0
        summons = any(re.search(p, t) for p in SUMMONS)
        if summons:
            parts["summons"] = 3.2
        if GREET.search(t):
            parts["greeting"] = 1.6                       # a bare "hello" is weak evidence on its own
        t2 = POLITE.sub("", t).strip()
        first = (t2.split() or [""])[0]
        if first in STARTERS:
            parts["request"] = 2.0
        elif first in Q_STARTS:
            parts["question"] = 1.0
        if REQUEST.search(t):
            parts["asks_you"] = 2.0
        topic_hits = len(set(toks) & TOPICS)
        if topic_hits:
            parts["topic"] = min(2.0, 1.2 * topic_hits)
        short = len(toks) <= 12                           # long sentences right after he spoke are less likely follow-ups
        if ctx.since_response < 15:
            parts["just_spoke"] = 3.0 if short else 1.2
        elif ctx.since_response < 45:
            parts["recent"] = 1.4 if short else 0.6
        elif ctx.since_response < 180:
            parts["lately"] = 0.5
        strong = bool(parts.keys() & {"name", "summons", "request", "asks_you"})
        if len(toks) <= 2 and not strong and ctx.since_response > 20:
            parts["too_short"] = -0.8
        if len(toks) > 40:
            parts["monologue"] = -3.0
        elif len(toks) > 22:
            parts["long"] = -1.5
        if VOCATIVES.search(t) or any(t.startswith(f"{pre}{n}") for n in ctx.people for pre in ("", "hey ", "hi ", "yo ")):
            parts["someone_else"] = -2.5
        if toks and all(w in FILLERS for w in toks) and ctx.since_response > 10:
            parts["filler"] = -3.5
        if ctx.meeting and "name" not in parts:
            parts["meeting"] = -3.5
        if ctx.media and "name" not in parts:
            parts["media"] = -1.0
        if ctx.asleep and not (parts.keys() & {"name", "summons"}):
            parts["asleep"] = -8.0
        return x + sum(parts.values()), parts

    def learned_logit(self, text: str) -> float:
        idx = self._idx(tokens(text))
        return float(self.w[idx].sum() / math.sqrt(len(idx)))

    def score(self, text: str, ctx: Context):
        base, parts = self.base_logit(text, ctx)
        return sigmoid(base + self.learned_logit(text)), parts

    def decide(self, text: str, ctx: Context):
        """('yes' | 'maybe' | 'no', probability, reasons)"""
        p, parts = self.score(text, ctx)
        verdict = "yes" if p >= self.hi else "maybe" if p >= self.lo else "no"
        return verdict, p, parts

    # ── learning ─────────────────────────────────────────────────
    def learn(self, text: str, label: float, weight: float = 1.0, ctx: Context = None) -> None:
        """label 1 = it was for Jarvis, 0 = it wasn't. weight: how sure we are (explicit corrections are strong)."""
        text = strip_address(text)
        if len(tokens(text)) < 1:
            return
        base, _ = self.base_logit(text, ctx or Context())
        idx = self._idx(tokens(text))
        with self.lock:
            p = sigmoid(base + float(self.w[idx].sum() / math.sqrt(len(idx))))
            step = 0.9 * weight * (label - p) / math.sqrt(len(idx))
            self.w[idx] = np.clip(self.w[idx] * 0.9995 + step, -3.0, 3.0)
            self.updates += 1
            if self.updates % 3 == 0 or weight >= 1.0:
                self.save()


# ── optional AI check for borderline sentences ───────────────────
JUDGE = ("You help a voice assistant named Jarvis decide whether the user just spoke to it or to someone else (a person in "
         "the room, a phone call, the TV, or themselves). Recent exchange with Jarvis: {recent}\nSentence: \"{text}\"\n"
         "Answer with exactly YES if it is most likely meant for the assistant, otherwise NO.")


def judge(client, model: str, text: str, recent: str = "(none)") -> bool:
    try:
        resp = client.models.generate_content(model=model, contents=JUDGE.format(text=text[:300], recent=recent[:300]))
        return (resp.text or "").strip().upper().startswith("YES")
    except Exception as exc:
        log.debug("judge unavailable: %s", exc)
        return False
