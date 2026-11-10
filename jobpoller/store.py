from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .migrations import migrate
from .models import STATUSES, Job, Ranking


@dataclass(frozen=True)
class QueuedJob:
    """A job that passed every filter and was queued; `id` is the stable CLI handle."""

    id: int
    key: str
    status: str
    status_updated: str
    first_seen: str
    company: str
    title: str
    url: str
    score: int  # keyword score
    source: str
    location: str
    fit: int | None  # LLM fit, None when ranking was off
    summary: str
    red_flags: tuple[str, ...]

    @property
    def sort_key(self) -> int:
        """LLM fit when available, otherwise the keyword score (mirrors Match.sort_key)."""
        return self.fit if self.fit is not None else self.score


_COLS = (
    "id, key, status, status_updated, first_seen, company, title, url, score, source, "
    "location, fit, summary, red_flags"
)


def _queued(row: tuple[object, ...]) -> QueuedJob:
    return QueuedJob(
        id=int(str(row[0])),
        key=str(row[1]),
        status=str(row[2]),
        status_updated=str(row[3]),
        first_seen=str(row[4]),
        company=str(row[5]),
        title=str(row[6]),
        url=str(row[7]),
        score=int(str(row[8])),
        source=str(row[9]),
        location=str(row[10]),
        fit=None if row[11] is None else int(str(row[11])),
        summary=str(row[12]),
        red_flags=tuple(json.loads(str(row[13]))),
    )


class Store:
    """Queued jobs and their status, the recent-jobs cache, and cached LLM rankings."""

    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(str(path))
        # Another process (scheduled run, TUI) may write at the same time: wait, don't fail.
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA journal_mode = WAL")
        migrate(self._conn)

    @contextmanager
    def _tx(self) -> Iterator[None]:
        try:
            yield
        except BaseException:
            self._conn.rollback()
            raise
        self._conn.commit()

    # --- queued jobs -------------------------------------------------------------------

    def add_if_new(
        self, job: Job, score: int, now: datetime, ranking: Ranking | None = None
    ) -> bool:
        with self._tx():
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO seen (key, first_seen, company, title, url, score, source, "
                "location, fit, summary, red_flags, status_updated, description, posted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job.key,
                    now.isoformat(),
                    job.company,
                    job.title,
                    job.url,
                    score,
                    job.source,
                    job.location,
                    ranking.fit if ranking else None,
                    ranking.summary if ranking else "",
                    json.dumps(list(ranking.red_flags)) if ranking else "[]",
                    now.isoformat(),
                    job.description,
                    job.posted_at.isoformat() if job.posted_at else None,
                ),
            )
        return cur.rowcount == 1

    def has(self, job: Job) -> bool:
        row = self._conn.execute("SELECT 1 FROM seen WHERE key = ?", (job.key,)).fetchone()
        return row is not None

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM seen").fetchone()
        return int(row[0])

    def get(self, job_id: int) -> QueuedJob | None:
        row = self._conn.execute(f"SELECT {_COLS} FROM seen WHERE id = ?", (job_id,)).fetchone()
        return _queued(row) if row else None

    def posting(self, job_id: int) -> tuple[str, str | None] | None:
        """(full posting text, posted_at ISO) for a queued job; kept apart from QueuedJob
        because the text is large and `list` never needs it."""
        row = self._conn.execute(
            "SELECT description, posted_at FROM seen WHERE id = ?", (job_id,)
        ).fetchone()
        return None if row is None else (str(row[0]), None if row[1] is None else str(row[1]))

    def list_jobs(self, status: str | None = None, min_fit: int | None = None) -> list[QueuedJob]:
        """Best first. `min_fit` and the ordering use LLM fit, else the keyword score."""
        rows = self._conn.execute(
            f"SELECT {_COLS} FROM seen "
            "WHERE (?1 IS NULL OR status = ?1) AND (?2 IS NULL OR COALESCE(fit, score) >= ?2) "
            "ORDER BY COALESCE(fit, score) DESC, id",
            (status, min_fit),
        ).fetchall()
        return [_queued(r) for r in rows]

    def set_status(self, ids: Iterable[int], status: str, now: datetime) -> list[int]:
        """Set status on existing ids; returns the ids that actually changed.

        One call is one undoable batch. Unknown ids and no-op changes are skipped.
        """
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
        batch = uuid.uuid4().hex
        changed: list[int] = []
        with self._tx():
            for job_id in dict.fromkeys(ids):  # de-duplicate, keep order
                row = self._conn.execute(
                    "SELECT status FROM seen WHERE id = ?", (job_id,)
                ).fetchone()
                if row is None or row[0] == status:
                    continue
                self._conn.execute(
                    "UPDATE seen SET status = ?, status_updated = ? WHERE id = ?",
                    (status, now.isoformat(), job_id),
                )
                self._conn.execute(
                    "INSERT INTO status_log (batch, seen_id, old_status, new_status, at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (batch, job_id, row[0], status, now.isoformat()),
                )
                changed.append(job_id)
        return changed

    def undo_last(self, now: datetime) -> list[int]:
        """Revert the most recent not-yet-undone status batch; returns the restored ids."""
        row = self._conn.execute(
            "SELECT batch FROM status_log WHERE undone = 0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return []
        restored: list[int] = []
        with self._tx():
            entries = self._conn.execute(
                "SELECT id, seen_id, old_status FROM status_log WHERE batch = ? AND undone = 0",
                (row[0],),
            ).fetchall()
            for log_id, seen_id, old in entries:
                self._conn.execute(
                    "UPDATE seen SET status = ?, status_updated = ? WHERE id = ?",
                    (old, now.isoformat(), seen_id),
                )
                self._conn.execute("UPDATE status_log SET undone = 1 WHERE id = ?", (log_id,))
                restored.append(int(seen_id))
        return restored

    def queued_keys(self) -> set[str]:
        return {r[0] for r in self._conn.execute("SELECT key FROM seen")}

    # --- small key/value store -----------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        with self._tx():
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
            )

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    # --- recent-jobs cache (powers offline re-scoring) ------------------------------------

    def cache_jobs(self, jobs: Iterable[Job], now: datetime) -> None:
        with self._tx():
            self._conn.executemany(
                "INSERT OR REPLACE INTO jobs_cache (key, fetched_at, job) VALUES (?, ?, ?)",
                [(j.key, now.isoformat(), j.to_json()) for j in jobs],
            )

    def prune_cache(self, now: datetime, days: int) -> int:
        cutoff = (now - timedelta(days=days)).isoformat()
        with self._tx():
            cur = self._conn.execute("DELETE FROM jobs_cache WHERE fetched_at < ?", (cutoff,))
        return cur.rowcount

    def cache_range(self) -> tuple[str, str] | None:
        """(oldest, newest) fetch time in the cache, or None when it is empty."""
        row = self._conn.execute(
            "SELECT MIN(fetched_at), MAX(fetched_at) FROM jobs_cache"
        ).fetchone()
        return None if row[0] is None else (str(row[0]), str(row[1]))

    def cached_jobs(self) -> list[Job]:
        rows = self._conn.execute("SELECT job FROM jobs_cache ORDER BY key").fetchall()
        return [Job.from_json(r[0]) for r in rows]

    # --- LLM ranking cache ---------------------------------------------------------------

    def get_ranking(self, job: Job, fingerprint: str) -> Ranking | None:
        row = self._conn.execute(
            "SELECT fit, summary, red_flags FROM ranking WHERE key = ? AND fingerprint = ?",
            (job.key, fingerprint),
        ).fetchone()
        if row is None:
            return None
        return Ranking(fit=int(row[0]), summary=row[1], red_flags=tuple(json.loads(row[2])))

    def put_ranking(self, job: Job, fingerprint: str, ranking: Ranking, now: datetime) -> None:
        with self._tx():
            self._conn.execute(
                "INSERT OR REPLACE INTO ranking "
                "(key, fingerprint, fit, summary, red_flags, created) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    job.key,
                    fingerprint,
                    ranking.fit,
                    ranking.summary,
                    json.dumps(list(ranking.red_flags)),
                    now.isoformat(),
                ),
            )

    def close(self) -> None:
        self._conn.close()
