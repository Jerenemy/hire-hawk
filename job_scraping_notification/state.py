"""Transactional snapshot and durable email outbox."""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import sqlite3


@contextmanager
def process_lock(database):
    path = Path(database).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another checker is already running") from exc
        try:
            yield path
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS current_jobs (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS seen_jobs (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY, payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                delivered_at TEXT
            );
        """)

    def close(self):
        self.db.close()

    def record(self, jobs, search_url):
        with self.db:
            stored = dict(self.db.execute("SELECT key, value FROM metadata"))
            if stored.get("search_url", search_url) != search_url:
                raise ValueError("SEARCH_URL changed; use a new DATABASE_PATH for a new search")
            first = "initialized" not in stored
            previous = {row[0] for row in self.db.execute("SELECT id FROM current_jobs")}
            seen = {row[0] for row in self.db.execute("SELECT id FROM seen_jobs")}
            new = [dict(job.to_dict(), returning=job.id in seen) for job in jobs if job.id not in previous]
            if first or new:
                self.db.execute("INSERT INTO outbox(payload) VALUES (?)", (json.dumps({
                    "initial": first, "jobs": new, "search_url": search_url,
                }),))
            self.db.execute("DELETE FROM current_jobs")
            self.db.executemany("INSERT INTO current_jobs(id) VALUES (?)", [(j.id,) for j in jobs])
            self.db.executemany("INSERT OR IGNORE INTO seen_jobs(id) VALUES (?)", [(j.id,) for j in jobs])
            self.db.executemany("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                                [("initialized", "1"), ("search_url", search_url)])
        return len(new)

    def pending(self):
        return [(row[0], json.loads(row[1]), row[2]) for row in self.db.execute(
            "SELECT id, payload, created_at FROM outbox WHERE delivered_at IS NULL ORDER BY id")]

    def delivered(self, notification_id):
        with self.db:
            self.db.execute("UPDATE outbox SET delivered_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (notification_id,))
