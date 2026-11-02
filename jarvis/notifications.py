"""Incoming messages from everywhere, via the notifications your Mac already receives.

Every WhatsApp, Instagram, Messages, Telegram, Slack... notification that appears on your Mac (including your
iPhone's, if "Allow notifications from iPhone" is on) is recorded by macOS in a local database. Jarvis reads that
database, so he can tell you who wrote and what they said without logging in to anything, without unofficial
APIs, and without any risk to your accounts. It is read-only and local; it needs Full Disk Access."""
import logging
import plistlib
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from .social import in_quiet_hours
from .util import DATA_DIR, notify

log = logging.getLogger("jarvis.notifications")
COCOA_EPOCH = 978307200            # macOS timestamps count seconds from 2001-01-01
INBOX_DB = DATA_DIR / "inbox.db"

SERVICES = [("whatsapp", "WhatsApp"), ("instagram", "Instagram"), ("burbn", "Instagram"), ("mobilesms", "Messages"),
            ("imessage", "Messages"), ("telegram", "Telegram"), ("telegra", "Telegram"), ("signal", "Signal"),
            ("slack", "Slack"), ("discord", "Discord"), ("snapchat", "Snapchat"), ("xinwechat", "WeChat"),
            ("twitter", "X"), ("linkedin", "LinkedIn"), ("apple.mail", "Mail"), ("outlook", "Outlook"),
            ("teams", "Teams"), ("messenger", "Messenger"), ("facebook", "Facebook")]
BROWSERS = ("chrome", "safari", "firefox", "arc", "brave", "edge", "opera")


def friendly_app(identifier: str, text: str = "") -> str:
    ident = (identifier or "").lower()
    for key, name in SERVICES:
        if key in ident:
            return name
    if any(b in ident for b in BROWSERS):          # web notifications: name the site if the text gives it away
        low = text.lower()
        for key, name in SERVICES:
            if key in low:
                return name
        return "Browser"
    return (identifier or "Unknown").split(".")[-1].title()


def db_candidates():
    paths = [Path.home() / "Library/Group Containers/group.com.apple.usernoted/db2/db"]   # macOS 15 and later
    try:
        base = subprocess.run(["getconf", "DARWIN_USER_DIR"], capture_output=True, text=True, timeout=3).stdout.strip()
        if base:
            paths.append(Path(base) / "com.apple.notificationcenter/db2/db")                # macOS 14 and earlier
    except Exception:
        pass
    return paths


def parse_record(blob):
    try:
        d = plistlib.loads(blob)
    except Exception:
        return None
    req = d.get("req") if isinstance(d, dict) and isinstance(d.get("req"), dict) else d
    if not isinstance(req, dict):
        return None
    title, sub, body = (str(req.get(k) or "").strip() for k in ("titl", "subt", "body"))
    return {"title": title, "subtitle": sub, "body": body} if (title or body) else None


def read_since(last_id: int):
    """(status, rows) where rows are (rec_id, app identifier, data blob, cocoa time). Reads a private copy of the database."""
    for path in db_candidates():
        if not path.exists():
            continue
        tmp = Path(tempfile.mkdtemp(prefix="jarvis-nc-"))
        try:
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(path) + suffix)
                if src.exists():
                    shutil.copy2(src, tmp / ("db" + suffix))
            con = sqlite3.connect(tmp / "db")
            rows = con.execute(
                "SELECT r.rec_id, a.identifier, r.data, COALESCE(r.delivered_date, r.request_date) FROM record r "
                "JOIN app a ON a.app_id = r.app_id WHERE r.rec_id > ? ORDER BY r.rec_id", (last_id,)).fetchall()
            con.close()
            return "ok", rows
        except PermissionError:
            return "needs_full_disk_access", []
        except OSError as exc:
            return ("needs_full_disk_access" if exc.errno in (1, 13) else "error"), []
        except sqlite3.Error as exc:
            log.warning("notification database unreadable: %s", exc)
            return "error", []
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return "no_database", []


class Inbox:
    """The messages Jarvis has seen arrive, kept locally for a while (config notifications.retention_days)."""

    def __init__(self, retention_days: int):
        self.db = sqlite3.connect(INBOX_DB, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("""CREATE TABLE IF NOT EXISTS inbox(id INTEGER PRIMARY KEY AUTOINCREMENT, rec_id INTEGER UNIQUE,
                               ts REAL, app TEXT, title TEXT, subtitle TEXT, body TEXT, announced INTEGER DEFAULT 0,
                               read INTEGER DEFAULT 0)""")
            self.db.execute("DELETE FROM inbox WHERE ts < ?", (time.time() - retention_days * 86400,))
            self.db.commit()

    def last_rec_id(self) -> int:
        with self.lock:
            return self.db.execute("SELECT COALESCE(MAX(rec_id), 0) FROM inbox").fetchone()[0]

    def add(self, rec_id, ts, app, title, subtitle, body, announced=0) -> None:
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO inbox(rec_id, ts, app, title, subtitle, body, announced) VALUES (?,?,?,?,?,?,?)",
                            (rec_id, ts, app, title, subtitle, body, announced))
            self.db.commit()

    def unannounced(self):
        with self.lock:
            return self.db.execute("SELECT id, ts, app, title, subtitle, body FROM inbox WHERE announced=0 ORDER BY ts").fetchall()

    def mark_announced(self, ids) -> None:
        with self.lock:
            self.db.executemany("UPDATE inbox SET announced=1 WHERE id=?", [(i,) for i in ids])
            self.db.commit()

    def unread(self, app: str = "", limit: int = 30):
        q, args = "SELECT id, ts, app, title, subtitle, body FROM inbox WHERE read=0", []
        if app:
            q += " AND LOWER(app)=?"
            args.append(app.lower())
        with self.lock:
            return self.db.execute(q + " ORDER BY ts DESC LIMIT ?", args + [limit]).fetchall()[::-1]

    def mark_read(self, ids) -> None:
        with self.lock:
            self.db.executemany("UPDATE inbox SET read=1, announced=1 WHERE id=?", [(i,) for i in ids])
            self.db.commit()


def _who(title: str, subtitle: str) -> str:
    return f"{subtitle} in {title}" if subtitle and title else (title or subtitle or "someone")


def compose(items, mode: str) -> str:
    """Spoken summary of new messages. mode: none | sender | full."""
    items = list(items)
    by_app = OrderedDict()
    for _id, _ts, app, title, sub, body in items:
        by_app.setdefault(app, []).append((_who(title, sub), body))
    if mode == "none":
        return f"{len(items)} new message{'s' if len(items) != 1 else ''}: " + ", ".join(f"{len(v)} on {a}" for a, v in by_app.items()) + "."
    parts = []
    for app, msgs in by_app.items():
        senders = OrderedDict()
        for who, body in msgs:
            senders.setdefault(who, []).append(body)
        if mode == "full":
            for who, bodies in list(senders.items())[:3]:
                parts.append(f"{who} on {app}: {bodies[-1][:90]}" + (f" (and {len(bodies) - 1} more)" if len(bodies) > 1 else ""))
        else:
            parts.append(", ".join(f"{len(b)} from {w}" for w, b in senders.items()) + f" on {app}")
    return "New messages: " + "; ".join(parts) + "."


class NotificationWatcher:
    def __init__(self, cfg, announce):
        self.cfg = cfg.notifications
        self.quiet = cfg.social.quiet_hours
        self.announce = announce
        self.enabled = bool(self.cfg.enabled)     # reading the macOS notification database
        self.announce_enabled = True              # speaking up about new messages (from any source); the menu toggles this
        self.status = "starting" if self.enabled else "disabled"   # ok | needs_full_disk_access | no_database | error | disabled
        self.inbox = Inbox(int(self.cfg.retention_days))
        self.allowed = {a.lower() for a in (self.cfg.apps or [])}
        self.last_id = None
        self._thread = None
        self._due = 0.0

    # ── reading ──────────────────────────────────────────────────
    def poll_once(self, now=None) -> int:
        now = now or time.time()
        first = self.last_id is None
        if first:
            self.last_id = self.inbox.last_rec_id()
        status, rows = read_since(self.last_id)
        self.status = status
        if status != "ok":
            return 0
        added = 0
        for rec_id, ident, blob, cocoa in rows:
            self.last_id = max(self.last_id, rec_id)
            rec = parse_record(blob)
            if not rec:
                continue
            text = f"{rec['title']} {rec['subtitle']} {rec['body']}"
            app = friendly_app(ident, text)
            if app.lower() not in self.allowed:
                continue
            ts = (cocoa + COCOA_EPOCH) if cocoa else now
            # on the very first look, quietly load the last hour so "what did I miss" works, but don't announce it
            if first and ts < now - 3600:
                continue
            self.inbox.add(rec_id, ts, app, rec["title"], rec["subtitle"], rec["body"], announced=1 if first else 0)
            added += 1
        if added and not first and not self._due:
            self._due = now + float(self.cfg.digest_seconds)   # bundle a burst into one announcement
        return added

    def maybe_announce(self, now=None) -> bool:
        now = now or time.time()
        if not self._due or now < self._due:
            return False
        if not self.announce_enabled or in_quiet_hours(self.quiet):
            return False                                       # keep them unannounced: "catch me up" later
        self._due = 0.0
        items = self.inbox.unannounced()
        if not items:
            return False
        text = compose(items, self.cfg.read_aloud)
        log.info("Messages: %s", text)
        notify("Jarvis: new messages", text[:200])
        self.announce(text)
        self.inbox.mark_announced([i[0] for i in items])
        return True

    def schedule(self) -> None:
        """Another source (the web inbox) added messages: announce them after the usual short bundling delay."""
        if not self._due:
            self._due = time.time() + float(self.cfg.digest_seconds)

    def start(self, force: bool = False) -> None:
        if self._thread is None and (self.cfg.enabled or force):
            self._thread = threading.Thread(target=self._loop, daemon=True, name="jarvis-notifications")
            self._thread.start()

    def _loop(self) -> None:
        while True:
            try:
                if self.enabled:
                    self.poll_once()
                self.maybe_announce()
            except Exception:
                log.exception("notification check failed")
            time.sleep(max(2.0, float(self.cfg.poll_seconds)))

    # ── asking ───────────────────────────────────────────────────
    def read(self, app: str = "", mark_read: bool = True, limit: int = 30) -> str:
        items = self.inbox.unread(app, limit)
        if not items and self.status in ("needs_full_disk_access", "no_database", "error"):
            return self.help_text()
        if not items:
            return "No unread messages."
        lines = [f"[{datetime.fromtimestamp(ts):%H:%M} {a}] {_who(t, s)}: {(b or '(no text)')[:200]}" for _i, ts, a, t, s, b in items]
        if mark_read:
            self.inbox.mark_read([i[0] for i in items])
        return "\n".join(lines)

    def spoken_summary(self) -> str:
        """For the instant 'any messages?' command."""
        items = self.inbox.unread()
        if items:
            return compose(items, self.cfg.read_aloud)
        return "No unread messages." if self.status in ("ok", "disabled") else self.help_text()

    def help_text(self) -> str:
        if self.status == "needs_full_disk_access":
            return "I can't read your notifications yet. Give Jarvis Full Disk Access in Privacy and Security."
        if self.status == "no_database":
            return "I can't find the macOS notification database on this Mac."
        return "I can't read your notifications right now."
