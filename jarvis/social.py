"""Message updates. Jarvis checks the unread counts your Mac already shows: Dock badges of messaging apps
(WhatsApp, Messages, Telegram, Slack...) and the "(3)" in the tab titles of sites you have open (Instagram,
WhatsApp Web, LinkedIn...). It tells you when the counts go up. It never logs in to anything for you, uses no
unofficial APIs, and only reads counts; reading the messages themselves is a separate, on-request job."""
import logging
import re
import subprocess
import threading
import time
from datetime import datetime

from .util import notify

log = logging.getLogger("jarvis.social")
BROWSERS = ["Safari", "Google Chrome", "Arc", "Brave Browser", "Microsoft Edge"]


def _run(cmd, timeout=8) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout


def _badge_number(label: str) -> int:
    m = re.match(r"\s*(\d+)", label or "")
    return int(m.group(1)) if m else (1 if (label or "").strip() else 0)   # a dot badge counts as 1


def dock_badge(app: str):
    """Unread badge on an app's Dock icon, or None if the app isn't running."""
    found = re.search(r"ASN:\S+", _run(["lsappinfo", "find", f"LSDisplayName={app}"]))
    if not found:
        return None
    m = re.search(r'"label"\s*=\s*"([^"]*)"', _run(["lsappinfo", "info", "-only", "StatusLabel", found.group(0)]))
    return _badge_number(m.group(1)) if m else 0


def tab_titles() -> list:
    """Titles of every open tab in the browsers that are running (never launches a browser)."""
    titles = []
    for browser in BROWSERS:
        if subprocess.run(["pgrep", "-x", browser], capture_output=True).returncode != 0:
            continue
        prop = "name" if browser == "Safari" else "title"
        script = (f'set out to ""\ntell application "{browser}"\nrepeat with w in windows\nrepeat with t in tabs of w\n'
                  f'set out to out & ({prop} of t) & linefeed\nend repeat\nend repeat\nend tell\nreturn out')
        try:
            titles += _run(["osascript", "-e", script], timeout=10).splitlines()
        except Exception as exc:
            log.debug("could not read %s tabs: %s", browser, exc)
    return titles


def web_counts(titles, sites) -> dict:
    out = {}
    for site in sites:
        total = 0
        for title in titles:
            m = re.search(r"\((\d+)\)", title)
            if m and site.lower() in title.lower():
                total += int(m.group(1))
        out[site.strip("/ ").title()] = total
    return out


def in_quiet_hours(window, now=None) -> bool:
    try:
        start, end = (datetime.strptime(t, "%H:%M").time() for t in window)
    except Exception:
        return False
    t = (now or datetime.now()).time()
    return (start <= t or t < end) if start > end else (start <= t < end)


class SocialWatcher:
    def __init__(self, cfg, announce):
        self.cfg = cfg.social
        self.announce = announce
        self.enabled = bool(self.cfg.enabled)
        self.last: dict = {}
        self._thread = None

    def snapshot(self) -> dict:
        """{display name: unread count}. The same service seen as an app and as a website counts once (the larger number)."""
        merged = {}                                   # lower-case key -> (display name, count)
        for app in self.cfg.apps or []:
            try:
                n = dock_badge(app)
            except Exception:
                n = None
            if n is not None:
                merged[app.lower()] = (app, n)
        if self.cfg.websites:
            try:
                for site, n in web_counts(tab_titles(), self.cfg.websites).items():
                    key = site.lower()
                    name, old = merged.get(key, (site, 0))
                    merged[key] = (name, max(old, n))
            except Exception as exc:
                log.debug("web check failed: %s", exc)
        return {name: n for name, n in merged.values()}

    def summary(self) -> str:
        snap = self.snapshot()
        if not snap:
            return "I can't see any of your messaging apps or sites open right now."
        unread = [f"{k} {v}" for k, v in snap.items() if v > 0]
        return ("Unread: " + ", ".join(unread) + ".") if unread else "No unread messages on the apps I can see."

    def start(self) -> None:
        if self._thread is None and self.cfg.enabled:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="jarvis-social")
            self._thread.start()

    def _loop(self) -> None:
        time.sleep(20)
        self.last = self.snapshot()           # baseline: what's already unread at startup is not "news"
        while True:
            time.sleep(max(1, float(self.cfg.check_minutes)) * 60)
            from .util import system_busy
            if system_busy():                      # don't add work while the Mac is busy
                continue
            try:
                snap = self.snapshot()
                new = {k: v - self.last.get(k, 0) for k, v in snap.items() if v > self.last.get(k, 0)}
                self.last = snap
                if new and self.enabled and not in_quiet_hours(self.cfg.quiet_hours):
                    msg = "Update: " + ", ".join(f"{n} new on {k}" for k, n in new.items()) + "."
                    log.info(msg)
                    notify("Jarvis", msg)
                    if self.cfg.speak:
                        self.announce(msg)
            except Exception:
                log.exception("message check failed")
