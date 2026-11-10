"""Schema versioning via `PRAGMA user_version`.

Each migration runs in its own transaction together with the version bump, so a crash
leaves the DB at the previous version, never half-migrated. Migrations are append-only:
never edit a shipped one, add the next number. A DB written by a newer jobpoller is refused
rather than silently downgraded.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable


class MigrationError(Exception):
    pass


def _m1_initial(conn: sqlite3.Connection) -> None:
    # Idempotent on purpose: pre-framework DBs (user_version 0) already have these tables.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen (
            key        TEXT PRIMARY KEY,
            first_seen TEXT NOT NULL,
            company    TEXT NOT NULL,
            title      TEXT NOT NULL,
            url        TEXT NOT NULL,
            score      INTEGER NOT NULL
        )
        """
    )
    conn.execute(
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
        """
    )


def _m2_status_and_cache(conn: sqlite3.Connection) -> None:
    # Rebuild `seen` so it gets a real INTEGER PRIMARY KEY: an implicit rowid is not stable
    # across VACUUM, and these ids are what the user types into the CLI.
    conn.execute(
        """
        CREATE TABLE seen_v2 (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            key            TEXT NOT NULL UNIQUE,
            first_seen     TEXT NOT NULL,
            company        TEXT NOT NULL,
            title          TEXT NOT NULL,
            url            TEXT NOT NULL,
            score          INTEGER NOT NULL,
            source         TEXT NOT NULL DEFAULT '',
            location       TEXT NOT NULL DEFAULT '',
            fit            INTEGER,
            summary        TEXT NOT NULL DEFAULT '',
            red_flags      TEXT NOT NULL DEFAULT '[]',
            status         TEXT NOT NULL DEFAULT 'new'
                CHECK (status IN ('new', 'promoted', 'applied', 'dismissed')),
            status_updated TEXT NOT NULL
        )
        """
    )
    # Keep old ids in first-seen order; backfill what we can from the ranking cache.
    conn.execute(
        """
        INSERT INTO seen_v2 (key, first_seen, company, title, url, score, source,
                             fit, summary, red_flags, status_updated)
        SELECT s.key, s.first_seen, s.company, s.title, s.url, s.score,
               substr(s.key, 1, instr(s.key, ':') - 1),
               (SELECT r.fit FROM ranking r WHERE r.key = s.key ORDER BY r.created DESC LIMIT 1),
               COALESCE((SELECT r.summary FROM ranking r WHERE r.key = s.key
                         ORDER BY r.created DESC LIMIT 1), ''),
               COALESCE((SELECT r.red_flags FROM ranking r WHERE r.key = s.key
                         ORDER BY r.created DESC LIMIT 1), '[]'),
               s.first_seen
        FROM seen s ORDER BY s.first_seen, s.rowid
        """
    )
    conn.execute("DROP TABLE seen")
    conn.execute("ALTER TABLE seen_v2 RENAME TO seen")
    conn.execute(
        """
        CREATE TABLE jobs_cache (
            key        TEXT PRIMARY KEY,
            fetched_at TEXT NOT NULL,
            job        TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX jobs_cache_fetched ON jobs_cache (fetched_at)")
    conn.execute(
        """
        CREATE TABLE status_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            batch      TEXT NOT NULL,
            seen_id    INTEGER NOT NULL,
            old_status TEXT NOT NULL,
            new_status TEXT NOT NULL,
            at         TEXT NOT NULL,
            undone     INTEGER NOT NULL DEFAULT 0
        )
        """
    )


def _m3_meta(conn: sqlite3.Connection) -> None:
    # Small key/value store; first user is the criteria snapshot `tune` diffs against.
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _m1_initial,
    _m2_status_and_cache,
    _m3_meta,
]


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the DB to the latest schema; returns the final version."""
    version = current_version(conn)
    if version > len(MIGRATIONS):
        raise MigrationError(
            f"database is schema v{version} but this jobpoller only knows v{len(MIGRATIONS)}; "
            "upgrade jobpoller (refusing to touch it)"
        )
    saved = conn.isolation_level
    conn.isolation_level = None  # manual BEGIN/COMMIT so DDL and the version bump are atomic
    try:
        for target in range(version + 1, len(MIGRATIONS) + 1):
            conn.execute("BEGIN IMMEDIATE")
            try:
                MIGRATIONS[target - 1](conn)
                conn.execute(f"PRAGMA user_version = {target}")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
    finally:
        conn.isolation_level = saved
    return len(MIGRATIONS)
