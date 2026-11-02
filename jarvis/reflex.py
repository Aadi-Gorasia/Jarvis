"""Instant commands. Simple requests ("what time is it", "open YouTube", "volume down", "timer for 10 minutes")
are handled right here in milliseconds with no AI call, so the basics never wait on a model."""
import logging
import re
import subprocess
from urllib.parse import quote_plus
from datetime import datetime

log = logging.getLogger("jarvis.reflex")

SITES = {"youtube": "https://www.youtube.com", "gmail": "https://mail.google.com", "google": "https://www.google.com",
         "github": "https://github.com", "maps": "https://maps.google.com", "netflix": "https://www.netflix.com",
         "chatgpt": "https://chatgpt.com", "claude": "https://claude.ai", "twitter": "https://x.com",
         "reddit": "https://www.reddit.com", "linkedin": "https://www.linkedin.com", "amazon": "https://www.amazon.in",
         "whatsapp": "https://web.whatsapp.com", "drive": "https://drive.google.com", "calendar": "https://calendar.google.com"}
BROWSERS = {"safari": "Safari", "chrome": "Google Chrome", "firefox": "Firefox", "brave": "Brave Browser", "arc": "Arc"}
WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
         "forty five": 45, "fifty": 50, "sixty": 60, "ninety": 90}
UNITS = {"second": 1, "minute": 60, "hour": 3600}


def clean(text: str) -> str:
    t = text.lower().replace("\u2019", "'")
    t = re.sub(r"'s\b", " s", t).replace("'", "")        # what's -> what s, don't -> dont
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = re.sub(r"^(hey |ok |okay )?jarvis\b", "", t.strip())
    t = re.sub(r"^\s*(please|can you|could you|would you|will you|i want you to|i need you to|go ahead and)\b", "", t.strip())
    t = re.sub(r"^\s*(please|can you|could you)\b", "", t.strip())
    t = re.sub(r"\b(please|jarvis|for me|right now|now|thanks|thank you)\s*$", "", t.strip())
    return re.sub(r"\s+", " ", t).strip()


def _num(token: str):
    token = token.strip()
    if token.isdigit():
        return int(token)
    return WORDS.get(token)


def _duration(amount: str, unit: str):
    n = _num(amount)
    return None if n is None else n * UNITS[unit.rstrip("s")]


def _spoken_duration(seconds: int) -> str:
    if seconds % 3600 == 0:
        n = seconds // 3600
        return f"{n} hour{'s' if n != 1 else ''}"
    if seconds % 60 == 0:
        n = seconds // 60
        return f"{n} minute{'s' if n != 1 else ''}"
    return f"{seconds} seconds"


def _osa(script: str) -> str:
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10).stdout.strip()


def _running(app: str) -> bool:
    return subprocess.run(["pgrep", "-x", app], capture_output=True).returncode == 0


def _music_app() -> str:
    return "Spotify" if _running("Spotify") else "Music"


# ── handlers: (match, jarvis) -> reply text, or None to hand the request to the AI ──
def _tools():
    from .tools import system_tools, web_tools
    return system_tools, web_tools


def h_time(m, j):
    return datetime.now().strftime("It's %-I:%M %p.")


def h_date(m, j):
    return datetime.now().strftime("It's %A, %-d %B.")


def h_open(m, j):
    target, browser = m.group("target").strip(), (m.groupdict().get("browser") or "").strip()
    if re.search(r"\b(and|then|search|for|to|that|with|about|my|the)\b", target) and target not in SITES:
        return None
    sys_t, _ = _tools()
    url = SITES.get(target) or (f"https://{target.replace(' ', '')}" if re.fullmatch(r"[a-z0-9-]+ (dot )?(com|org|net|in|io)", target) else None)
    if re.fullmatch(r"[a-z0-9-]+ dot (com|org|net|in|io)", target):
        url = "https://" + target.replace(" dot ", ".")
    if url:
        if browser in BROWSERS:
            subprocess.run(["open", "-a", BROWSERS[browser], url])
        else:
            sys_t.open_url(url)
        return f"Opening {target.split(' dot ')[0] if ' dot ' in target else target}."
    result = sys_t.open_app(target.title() if target.islower() else target)
    return f"Opening {target}." if not result.startswith("Error") else None  # unknown app: let the AI figure it out


def h_volume_set(m, j):
    _tools()[0].set_volume(int(m.group("n")))
    return f"Volume {m.group('n')}."


def h_volume_step(m, j):
    try:
        cur = int(_osa("output volume of (get volume settings)") or 50)
    except ValueError:
        cur = 50
    d = m.groupdict()
    new = max(0, min(100, cur + (10 if (d.get("dir") or d.get("dir2")) in ("up", "louder") else -10)))
    _tools()[0].set_volume(new)
    return f"Volume {new}."


def h_mute(m, j):
    _osa("set volume with output muted" if m.group("mute") == "mute" else "set volume without output muted")
    return "Muted." if m.group("mute") == "mute" else "Unmuted."


def h_music(m, j):
    act = {"pause": "pause", "stop": "pause", "play": "play", "resume": "play", "next": "next", "skip": "next",
           "previous": "previous", "last": "previous"}[m.group("act")]
    res = _tools()[0].media_control(act, _music_app())
    return {"pause": "Paused.", "play": "Playing.", "next": "Next track.", "previous": "Previous track."}[act] if not res.startswith("Error") else None


def h_timer(m, j):
    secs = _duration(m.group("n"), m.group("unit"))
    if not secs or secs > 86400:
        return None
    label = (m.groupdict().get("label") or "").strip()
    name = f"reminder to {label}" if label else "timer"
    _tools()[0].set_timer(secs, name)
    return f"{'Reminder' if label else 'Timer'} set for {_spoken_duration(secs)}."


def h_weather(m, j):
    city = (m.groupdict().get("city") or "").strip()
    out = _tools()[1].get_weather(city)
    return None if out.startswith("Error") else out


def h_battery(m, j):
    out = _tools()[0].system_status()
    if out.startswith("Error"):
        return None
    if "battery" in m.string:                      # asked about the battery: answer just that
        b = re.search(r"Battery [^.]*\.", out)
        return b.group(0) if b else "I can't see a battery on this Mac."
    return out


def _spoken_elapsed(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60} minute{'s' if sec // 60 != 1 else ''} {sec % 60} seconds" if sec >= 60 else f"{sec} seconds"


def _launch(j, agent: str, brief: str, m, what: str):
    auto = bool(re.search(r"dont ask|do not ask|go ahead|everything|without asking|no need to ask|just do it", m.string))
    t = j.manager.spawn(agent, brief + f' (The user said: "{m.string}")', auto_approve=auto)
    return (f"On it: task {t.id}, {what}. Watch the timer on screen, and I'll tell you when it's done."
            + (" I won't ask for approvals." if auto else ""))


def h_github(m, j):
    return _launch(j, "github", "Do the full GitHub cleanup following your workflow: audit, plan, fix locally on a branch, commit, push, open pull requests, and make the profile and flagship repositories look professional.", m, "the GitHub agent")


def h_clean_mac(m, j):
    folder = re.search(r"(downloads|desktop)", m.string)
    brief = (f"Organise the user's {folder.group(1).title()} folder into sensible subfolders using move_many (undoable), without touching anything modified this week."
             if folder and "organi" in m.string else
             "Do the full Mac cleanup following your workflow: scan everything, move the clearly safe items to the Trash in one batch, and report the space freed and what needs the user's decision.")
    return _launch(j, "cleaner", brief, m, "the cleaner agent")


def h_agents(m, j):
    jobs = j.manager.jobs()
    if jobs:
        return "Working on: " + "; ".join(f"{agent}, {_spoken_elapsed(el)}, {detail}" for _tid, agent, el, detail in jobs[:3]) + "."
    tasks = list(j.manager.tasks.values())
    if tasks:
        last = max(tasks, key=lambda t: t.id)
        return f"Nothing is running. Task {last.id}, the {last.agent} agent, is {last.status}."
    return "Nothing is running and there are no tasks yet."


def h_cancel(m, j):
    return j.manager.cancel(int(m.group("n")))


def h_approve(m, j):
    return "Approved." if "Approved" in j.broker.resolve(0, True) else None


def h_deny(m, j):
    return "Denied." if "Denied" in j.broker.resolve(0, False) else None


def h_messages(m, j):
    n, soc = getattr(j, "notifier", None), getattr(j, "social", None)
    if n is not None:
        items = n.inbox.unread()
        if items:
            return n.spoken_summary()
    if soc is not None:                                  # counts from Dock badges and tab titles
        counts = soc.summary()
        w = getattr(j, "webinbox", None)
        hint = (" " + w.help_text()) if (w is not None and w.status not in ("ok", "disabled", "starting") and "Unread" not in counts) else ""
        return counts + hint
    return n.help_text() if n is not None else None


def h_catchup(m, j):
    n = getattr(j, "notifier", None)
    if n is None:
        return None
    total = len(n.inbox.unread(limit=1000))
    items = n.inbox.unread(limit=5)
    if not items:
        return "You're all caught up."
    from .notifications import _who
    n.inbox.mark_read([i[0] for i in items])
    text = " ".join(f"{_who(t, s)} on {a}: {(b or 'sent something')[:100]}." for _i, _ts, a, t, s, b in items)
    return text + (f" And {total - len(items)} more." if total > len(items) else "")


def h_google(m, j):
    q = m.group("q").strip()
    _tools()[0].open_url("https://www.google.com/search?q=" + quote_plus(q))
    return f"Searching Google for {q}."


def h_youtube(m, j):
    q = (m.group("q") or m.group("q2") or "").strip()
    _tools()[0].open_url("https://www.youtube.com/results?search_query=" + quote_plus(q))
    return f"Searching YouTube for {q}."


def h_spotify(m, j):
    q = m.group("q").strip()
    subprocess.run(["open", "spotify:search:" + quote_plus(q)])
    return f"Searching Spotify for {q}."


def h_know(m, j):
    mem = getattr(j, "memory", None)
    if mem is None:
        return None
    facts = mem.all_facts(limit=40)
    if not facts:
        return "I haven't learned anything about you yet."
    shown = "; ".join(f"{t}: {d}" for t, d in facts[:5])
    return f"I know {len(facts)} things about you. The latest: {shown}." + (" Say forget, then a topic, to remove one." if len(facts) else "")


def h_undo_cleanup(m, j):
    from .tools import cleanup_tools
    return cleanup_tools.undo_cleanup("")


def h_pause(m, j):
    j.paused = True
    return "Pausing. Use the menu bar to wake me."


RULES = [
    (re.compile(r"(yes|yeah|yep|approve|approved|allow it|go ahead|do it)( .*)?"), h_approve, True),
    (re.compile(r"(no|nope|deny|denied|reject|dont|cancel|stop)( .*)?"), h_deny, True),
    (re.compile(r"(what time is it|what s the time|what is the time|tell me the time|current time|time|what time)"), h_time, False),
    (re.compile(r"(what s the date|what is the date|what day is it|what s today s date|today s date|what s today|date)"), h_date, False),
    (re.compile(r"(open|launch) (?P<target>[a-z0-9 ]{2,30}?)( (in|on|with) (?P<browser>safari|chrome|firefox|brave|arc))?"), h_open, False),
    (re.compile(r"(set |turn )?(the )?volume (to|at) (?P<n>\d{1,3})( percent)?"), h_volume_set, False),
    (re.compile(r"(turn |set )?(the )?volume (?P<dir>up|down)|(make it |turn it )?(?P<dir2>louder|quieter)"), h_volume_step, False),
    (re.compile(r"(?P<mute>mute|unmute)( (the )?(sound|volume|mac|computer))?"), h_mute, False),
    (re.compile(r"(?P<act>pause|stop|play|resume|next|skip|previous|last)( the| this)?( (song|track|music|playback))?"), h_music, False),
    (re.compile(r"(set )?(a |an )?timer (for )?(?P<n>\d+|[a-z ]+?) (?P<unit>seconds?|minutes?|hours?)"), h_timer, False),
    (re.compile(r"(set )?(a )?(?P<n>\d+|[a-z]+) (?P<unit>seconds?|minutes?|hours?) timer"), h_timer, False),
    (re.compile(r"remind me in (?P<n>\d+|[a-z]+) (?P<unit>seconds?|minutes?|hours?) to (?P<label>.+)"), h_timer, False),
    (re.compile(r"(what s |what is |how s |how is )?(the )?weather( like)?( today)?( in (?P<city>[a-z ]+))?"), h_weather, False),
    (re.compile(r"(what s |how s |how is |check )?(my )?(battery|battery level|battery life)( level)?|how much battery( do i have)?|how s my (mac|computer)"), h_battery, False),
    (re.compile(r"(what are (the )?(agents|you|everyone|they) (doing|working on)|what s everyone doing|status( report)?|any updates|task status)"), h_agents, False),
    (re.compile(r"(cancel|stop) task (?P<n>\d+)"), h_cancel, False),
    (re.compile(r"(what did i miss|catch me up|read my (messages|notifications|texts)|read (them|my inbox)|what did they say)"), h_catchup, False),
    (re.compile(r"(what s |what is )?(any |do i have any |do i have |check my |check |show my |what are my )?(new |unread )?(messages|notifications|texts|dms|whatsapp|instagram)( updates?)?|what s new|anything new"), h_messages, False),
    (re.compile(r"(google|search google for|search the web for) (?P<q>[a-z0-9 ]{2,60})"), h_google, False),
    (re.compile(r"(play |search youtube for |youtube )(?P<q>[a-z0-9 ]{2,60}?)( on youtube| in youtube)|search youtube for (?P<q2>[a-z0-9 ]{2,60})"), h_youtube, False),
    (re.compile(r"play (?P<q>[a-z0-9 ]{2,60}?) on spotify"), h_spotify, False),
    (re.compile(r"(what do you (know|remember) about me|what have you learned( about me)?|what s in your memory|what do you know)"), h_know, False),
    (re.compile(r"(clean|clean up|tidy|tidy up|audit|fix|sort out|organi[sz]e|spruce up|polish)( up)? my (github|git hub|repos|repositories|github profile|github account|git hub profile)( and .*)?"), h_github, False),
    (re.compile(r"((clean|clean up|tidy|tidy up|declutter|speed up|organi[sz]e)( up)? my (mac|macbook|laptop|computer|files|disk|storage|desktop|downloads)|free up (some |more )?(space|storage)( on my (mac|laptop|computer))?)( and .*)?"), h_clean_mac, False),
    (re.compile(r"(undo|revert) (the |that |my )?(cleanup|clean up|clean|reorganis[a-z]+|organi[sz]ing)"), h_undo_cleanup, False),
    (re.compile(r"(pause listening|stop listening|mute yourself|go quiet)"), h_pause, False),
]


def handle(text: str, jarvis):
    """Return a reply if this is a simple built-in command, else None (hand it to the AI)."""
    t = clean(text)
    if not t or len(t.split()) > 9:
        return None
    for pattern, fn, needs_pending in RULES:
        if needs_pending and not jarvis.broker.pending:
            continue
        m = pattern.fullmatch(t)
        if not m:
            continue
        try:
            reply = fn(m, jarvis)
        except Exception:
            log.exception("reflex handler failed; handing over to the AI")
            return None
        if reply is not None:
            return reply
    return None
