"""Your ChatGPT and Gemini history, imported from the exports those services give you, kept in a local
searchable database (~/.jarvis/history.db). Neither service offers a live "read my chats" API, so this works
from your data exports; re-import a fresh export any time (duplicates are skipped)."""
import hashlib
import html
import io
import json
import logging
import re
import sqlite3
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .util import DATA_DIR

log = logging.getLogger("jarvis.history")
DB_PATH = DATA_DIR / "history.db"


def _connect():
    db = sqlite3.connect(DB_PATH)
    try:
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS msgs USING fts5(text, source UNINDEXED, title UNINDEXED, ts UNINDEXED, role UNINDEXED)")
        fts = True
    except sqlite3.OperationalError:     # Python build without FTS5: fall back to plain LIKE search
        db.execute("CREATE TABLE IF NOT EXISTS msgs(text, source, title, ts, role)")
        fts = False
    db.execute("CREATE TABLE IF NOT EXISTS seen(h TEXT PRIMARY KEY)")
    return db, fts


# ── reading the export files ─────────────────────────────────────
def _blobs(path: Path):
    """Yield (file name, text) for every file inside a folder, a .zip, or a single file."""
    path = Path(path).expanduser()
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.lower().endswith((".json", ".html")):
                    yield name, z.read(name).decode("utf-8", errors="replace")
    elif path.is_dir():
        for f in path.rglob("*"):
            if f.suffix.lower() in (".json", ".html") and f.is_file():
                yield str(f), f.read_text(encoding="utf-8", errors="replace")
    elif path.is_file():
        yield str(path), path.read_text(encoding="utf-8", errors="replace")


def parse_chatgpt(text: str):
    """ChatGPT export: conversations.json (a list of conversations, each a tree of messages)."""
    for conv in json.loads(text):
        title = conv.get("title") or "Untitled"
        for node in (conv.get("mapping") or {}).values():
            m = node.get("message")
            if not m or (m.get("metadata") or {}).get("is_visually_hidden_from_conversation"):
                continue
            role = (m.get("author") or {}).get("role")
            if role not in ("user", "assistant"):
                continue
            content = m.get("content") or {}
            parts = [p for p in (content.get("parts") or []) if isinstance(p, str)]
            body = "\n".join(parts).strip() or (content.get("text") or "").strip()
            if body:
                yield "chatgpt", title, m.get("create_time") or conv.get("create_time") or 0, role, body


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _iso(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def parse_gemini(text: str):
    """Google Takeout > My Activity > Gemini Apps > MyActivity.json."""
    for e in json.loads(text):
        title = e.get("title") or ""
        if not title.startswith("Prompted "):
            continue
        ts = _iso(e.get("time", ""))
        yield "gemini", title[9:60], ts, "user", title[9:].strip()
        reply = _strip_html("".join(i.get("html", "") for i in (e.get("safeHtmlItem") or [])))
        if reply:
            yield "gemini", title[9:60], ts, "assistant", reply


def import_paths(paths) -> dict:
    db, _ = _connect()
    counts = {"chatgpt": 0, "gemini": 0, "skipped_duplicates": 0}
    for p in paths:
        for name, text in _blobs(Path(p)):
            base = Path(name).name.lower()
            try:
                if re.fullmatch(r"conversations(-\d+)?\.json", base):
                    rows = parse_chatgpt(text)
                elif base == "myactivity.json" and "gemini" in name.lower():
                    rows = parse_gemini(text)
                else:
                    continue
                for source, title, ts, role, body in rows:
                    h = hashlib.sha1(f"{source}|{ts}|{role}|{body}".encode()).hexdigest()
                    if db.execute("SELECT 1 FROM seen WHERE h=?", (h,)).fetchone():
                        counts["skipped_duplicates"] += 1
                        continue
                    db.execute("INSERT INTO msgs(text, source, title, ts, role) VALUES (?,?,?,?,?)",
                               (body, source, title, float(ts or 0), role))
                    db.execute("INSERT INTO seen VALUES (?)", (h,))
                    counts[source] += 1
            except Exception as exc:
                log.warning("could not read %s (%s)", name, str(exc)[:100])
    db.commit()
    db.close()
    return counts


# ── searching ────────────────────────────────────────────────────
def search(query: str, source: str = "", limit: int = 6):
    db, fts = _connect()
    words = re.findall(r"\w{2,}", query.lower())[:8]
    if not words:
        return []
    args, where = [], ""
    if source in ("chatgpt", "gemini", "jarvis"):
        where = " AND source = ?"
        args = [source]
    if fts:
        sql = f"SELECT text, source, title, ts, role FROM msgs WHERE msgs MATCH ?{where} ORDER BY bm25(msgs) LIMIT ?"
        rows = db.execute(sql, [" OR ".join(words)] + args + [limit]).fetchall()
    else:
        like = " OR ".join("text LIKE ?" for _ in words)
        rows = db.execute(f"SELECT text, source, title, ts, role FROM msgs WHERE ({like}){where} LIMIT ?",
                          [f"%{w}%" for w in words] + args + [limit]).fetchall()
    db.close()
    return rows


def stats() -> str:
    db, _ = _connect()
    rows = db.execute("SELECT source, COUNT(*) FROM msgs GROUP BY source").fetchall()
    db.close()
    return ", ".join(f"{n:,} {s} messages" for s, n in rows) or "nothing imported yet"


def format_rows(rows, width: int = 360) -> str:
    out = []
    for text, source, title, ts, role in rows:
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else "undated"
        snippet = re.sub(r"\s+", " ", text)[:width] + ("..." if len(text) > width else "")
        out.append(f"[{date} {source} '{title[:40]}'] {role}: {snippet}")
    return "\n".join(out)


# ── distilling the history into long-term facts ──────────────────
def distill(cfg, client, memory, ask=input) -> int:
    """Read the most recent things you asked chatbots, propose lasting facts about you, save the ones you approve."""
    db, _ = _connect()
    rows = db.execute("SELECT text FROM msgs WHERE role='user' ORDER BY ts DESC LIMIT 250").fetchall()
    db.close()
    if not rows:
        print("Nothing imported yet. Run ./jarvisctl.sh import-history <export> first.")
        return 0
    sample = "\n".join("- " + re.sub(r"\s+", " ", r[0])[:240] for r in rows)
    prompt = ("Below are messages one person recently sent to AI chatbots. Extract up to 40 durable facts that would help a "
              "personal assistant: ongoing projects, interests, skills, tools they use, preferences, goals, places and "
              "organisations they mention often. EXCLUDE anything about health, money, relationships or private life, and any "
              "passwords, IDs or contact details. Reply with ONLY a JSON array of {\"topic\": short label, \"detail\": one sentence}.\n\n"
              + sample)
    resp = client.models.generate_content(model=cfg.gemini.model, contents=prompt)
    m = re.search(r"\[.*\]", resp.text or "", re.S)
    facts = json.loads(m.group(0)) if m else []
    facts = [f for f in facts if isinstance(f, dict) and f.get("topic") and f.get("detail")][:40]
    if not facts:
        print("The model proposed no facts.")
        return 0
    print("\nProposed facts for Jarvis's memory:\n")
    for f in facts:
        print(f"  - {f['topic']}: {f['detail']}")
    if not ask("\nSave these? [y/N] ").strip().lower().startswith("y"):
        print("Nothing saved.")
        return 0
    for f in facts:
        memory.remember(str(f["topic"]), str(f["detail"]))
    return len(facts)


# ── automatic import: drop (or download) a new export and Jarvis picks it up ──
def looks_like_export(path: Path) -> bool:
    """Peek inside a zip without extracting it: a ChatGPT export, or a Google Takeout holding Gemini activity."""
    try:
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                base = Path(n).name.lower()
                if re.fullmatch(r"conversations(-\d+)?\.json", base) or (base == "myactivity.json" and "gemini" in n.lower()):
                    return True
    except Exception:
        return False
    return False



def _defer() -> bool:
    """Skip this round when the Mac is busy: background checks should never compete with your own work."""
    from .util import system_busy
    return system_busy()


class HistoryWatcher:
    """Every few minutes, look in your Downloads (and a drop folder) for new ChatGPT/Gemini exports and import them."""

    def __init__(self, cfg, announce):
        self.cfg = cfg.history
        self.announce = announce
        self.dirs = [Path(d).expanduser() for d in (self.cfg.watch or [])]
        self.done = set()
        self._thread = None

    def scan(self) -> int:
        total = 0
        for d in self.dirs:
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.zip")):
                key = (str(f), f.stat().st_mtime)
                if key in self.done:
                    continue
                self.done.add(key)
                if not looks_like_export(f):
                    continue
                counts = import_paths([f])
                n = counts["chatgpt"] + counts["gemini"]
                if n:
                    total += n
                    which = "ChatGPT" if counts["chatgpt"] else "Gemini"
                    log.info("Imported %d messages from %s", n, f.name)
                    self.announce(f"I imported {n:,} message{'s' if n != 1 else ''} from your new {which} export.")
        return total

    def start(self) -> None:
        if self._thread is None and self.cfg.auto_import:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="jarvis-history")
            self._thread.start()

    def _loop(self) -> None:
        while True:
            try:
                if not _defer():
                    self.scan()
            except Exception:
                log.exception("history scan failed")
            time.sleep(max(1.0, float(self.cfg.scan_minutes)) * 60)


# ── Jarvis's own conversations join the same searchable history ──
def add(source: str, title: str, ts: float, role: str, body: str) -> None:
    body = (body or "").strip()
    if not body:
        return
    db, _ = _connect()
    try:
        h = hashlib.sha1(f"{source}|{ts}|{role}|{body}".encode()).hexdigest()
        if not db.execute("SELECT 1 FROM seen WHERE h=?", (h,)).fetchone():
            db.execute("INSERT INTO msgs(text, source, title, ts, role) VALUES (?,?,?,?,?)", (body, source, title[:60], float(ts), role))
            db.execute("INSERT INTO seen VALUES (?)", (h,))
            db.commit()
    finally:
        db.close()


STOP = set("""the and but for with about from this that these those what when where who how why tell give show let know like want
need get make just please can could would should will have has had are was were been you your our their they them his her its
did does done any some more most very really also than then there here into out over under again once now still""".split())


def context_for(query: str, limit: int = 3, skip_recent_seconds: int = 900) -> str:
    """Passages from past ChatGPT, Gemini and Jarvis conversations that genuinely relate to this request (or "").
    A passage must share at least two meaningful words with the request, so unrelated chatter is never injected."""
    words = list(dict.fromkeys(w for w in re.findall(r"[a-z0-9]{3,}", query.lower()) if w not in STOP))
    if len(words) < 2:
        return ""
    db, fts = _connect()
    try:
        if not fts:
            return ""
        rows = db.execute("SELECT text, source, title, ts, role FROM msgs WHERE msgs MATCH ? ORDER BY bm25(msgs) LIMIT 15",
                          (" OR ".join(words[:8]),)).fetchall()
    except sqlite3.Error:
        return ""
    finally:
        db.close()
    now, scored = time.time(), []
    for row in rows:
        text, source, _title, ts, _role = row
        if source == "jarvis" and ts > now - skip_recent_seconds:
            continue                                   # this very conversation: Jarvis already has it
        hits = sum(1 for w in words if w in text.lower())
        if hits >= min(2, len(words)):
            scored.append((hits, row))
    scored.sort(key=lambda x: -x[0])
    return format_rows([r for _h, r in scored[:limit]], width=260)
