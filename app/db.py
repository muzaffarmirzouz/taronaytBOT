"""SQLite storage: the song queue and a tiny key/value table."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'queued',
    source TEXT NOT NULL DEFAULT 'auto',
    raw_text TEXT DEFAULT '',
    theme TEXT DEFAULT '',
    title TEXT DEFAULT '',
    lyrics TEXT DEFAULT '',
    style TEXT DEFAULT '',
    image_prompt TEXT DEFAULT '',
    yt_title TEXT DEFAULT '',
    yt_description TEXT DEFAULT '',
    yt_tags TEXT DEFAULT '[]',
    mp3_path TEXT DEFAULT '',
    image_path TEXT DEFAULT '',
    mp4_path TEXT DEFAULT '',
    youtube_id TEXT DEFAULT '',
    error TEXT DEFAULT '',
    attempts INTEGER DEFAULT 0,
    created_at REAL,
    posted_at REAL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

FIELDS = {
    "status", "source", "raw_text", "theme", "title", "lyrics", "style", "image_prompt",
    "yt_title", "yt_description", "yt_tags", "mp3_path", "image_path", "mp4_path",
    "youtube_id", "error", "attempts", "posted_at",
}

# Statuses meaning "a video exists or is on its way".
STOCK_STATUSES = ("queued", "producing", "ready")


class DB:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        self._recover()

    def _recover(self) -> None:
        """After a crash/redeploy, put half-finished jobs back in a sane state."""
        with self.lock:
            self.conn.execute("UPDATE jobs SET status='queued' WHERE status='producing'")
            self.conn.execute("UPDATE jobs SET status='posted' WHERE status='posting' AND youtube_id != ''")
            self.conn.execute("UPDATE jobs SET status='ready' WHERE status='posting'")
            self.conn.commit()

    # --- jobs ---------------------------------------------------------
    def add_job(self, source: str, raw_text: str = "", theme: str = "", status: str = "queued") -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO jobs (source, raw_text, theme, status, created_at) VALUES (?, ?, ?, ?, ?)",
                (source, raw_text, theme, status, time.time()),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def get(self, job_id: int):
        with self.lock:
            row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def update(self, job_id: int, **fields) -> None:
        bad = set(fields) - FIELDS
        if bad:
            raise ValueError(f"unknown job fields: {bad}")
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.lock:
            self.conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
            self.conn.commit()

    def next_to_produce(self):
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM jobs WHERE status='queued' "
                "ORDER BY (source='user') DESC, id ASC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def next_ready(self):
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM jobs WHERE status='ready' "
                "ORDER BY (source='user') DESC, id ASC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def count(self, *statuses: str) -> int:
        if not statuses:
            return 0
        marks = ",".join("?" for _ in statuses)
        with self.lock:
            return int(self.conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE status IN ({marks})", statuses
            ).fetchone()[0])

    def recent(self, n: int = 10) -> list:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (n,)).fetchall()
            return [dict(r) for r in rows]

    def recent_titles(self, n: int = 25) -> list:
        with self.lock:
            rows = self.conn.execute(
                "SELECT title FROM jobs WHERE title != '' ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
            return [r["title"] for r in rows]

    def recent_themes(self, n: int = 6) -> list:
        with self.lock:
            rows = self.conn.execute(
                "SELECT theme FROM jobs WHERE theme != '' ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
            return [r["theme"] for r in rows]

    # --- key/value ------------------------------------------------------
    def kv_get(self, key: str, default=None):
        with self.lock:
            row = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
            return row["v"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, str(value)),
            )
            self.conn.commit()
