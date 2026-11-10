from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import Job, Ranking

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS seen (
        key        TEXT PRIMARY KEY,
        first_seen TEXT NOT NULL,
        company    TEXT NOT NULL,
        title      TEXT NOT NULL,
        url        TEXT NOT NULL,
        score      INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ranking (
        key         TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        fit         INTEGER NOT NULL,
        summary     TEXT NOT NULL,
        red_flags   TEXT NOT NULL,
        created     TEXT NOT NULL,
        PRIMARY KEY (key, fingerprint)
    )
    """,
)


class Store:
    """Remembers queued jobs (so reruns only surface new ones) and cached LLM rankings."""

    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(str(path))
        for statement in _SCHEMA:
            self._conn.execute(statement)
        self._conn.commit()

    def add_if_new(self, job: Job, score: int, now: datetime) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO seen (key, first_seen, company, title, url, score) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job.key, now.isoformat(), job.company, job.title, job.url, score),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def has(self, job: Job) -> bool:
        row = self._conn.execute("SELECT 1 FROM seen WHERE key = ?", (job.key,)).fetchone()
        return row is not None

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM seen").fetchone()
        return int(row[0])

    def get_ranking(self, job: Job, fingerprint: str) -> Ranking | None:
        row = self._conn.execute(
            "SELECT fit, summary, red_flags FROM ranking WHERE key = ? AND fingerprint = ?",
            (job.key, fingerprint),
        ).fetchone()
        if row is None:
            return None
        return Ranking(fit=int(row[0]), summary=row[1], red_flags=tuple(json.loads(row[2])))

    def put_ranking(self, job: Job, fingerprint: str, ranking: Ranking, now: datetime) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO ranking (key, fingerprint, fit, summary, red_flags, created) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                job.key,
                fingerprint,
                ranking.fit,
                ranking.summary,
                json.dumps(list(ranking.red_flags)),
                now.isoformat(),
            ),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
