import sqlite3
import threading
import time


class Memory:
    """Tiny SQLite store: long-term facts + a conversation log."""

    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS facts(
                    topic TEXT PRIMARY KEY, detail TEXT NOT NULL, updated REAL);
                CREATE TABLE IF NOT EXISTS log(ts REAL, role TEXT, text TEXT);
                """
            )

    def remember(self, topic: str, detail: str) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO facts(topic, detail, updated) VALUES(?,?,?) "
                "ON CONFLICT(topic) DO UPDATE SET detail=excluded.detail, updated=excluded.updated",
                (topic.strip().lower(), detail.strip(), time.time()),
            )
            self.db.commit()

    def forget(self, topic: str) -> bool:
        with self.lock:
            cur = self.db.execute("DELETE FROM facts WHERE topic LIKE ?", (f"%{topic.strip().lower()}%",))
            self.db.commit()
            return cur.rowcount > 0

    def search(self, query: str, limit: int = 10):
        like = f"%{query.strip().lower()}%"
        with self.lock:
            return self.db.execute(
                "SELECT topic, detail FROM facts WHERE topic LIKE ? OR detail LIKE ? LIMIT ?",
                (like, like, limit),
            ).fetchall()

    def all_facts(self, limit: int = 60):
        with self.lock:
            return self.db.execute(
                "SELECT topic, detail FROM facts ORDER BY updated DESC LIMIT ?", (limit,)
            ).fetchall()

    def log(self, role: str, text: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO log VALUES(?,?,?)", (time.time(), role, text))
            self.db.commit()

    def recent(self, n: int = 8):
        """The last n chat messages, oldest first, as (role, text)."""
        with self.lock:
            rows = self.db.execute("SELECT role, text FROM log ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
        return rows[::-1]
