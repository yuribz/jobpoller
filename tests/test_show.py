"""`jobpoller show` and the posting text kept for queued jobs (the companion-tool contract)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jobpoller.cli import SHOW_SCHEMA_VERSION
from jobpoller.config import load_config
from jobpoller.migrations import MIGRATIONS
from jobpoller.models import Job
from jobpoller.store import Store
from tests.test_workflow import cli, populated  # noqa: F401  (fixture)

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def test_show_json_has_the_documented_contract(
    populated: Path, capsys: pytest.CaptureFixture[str]  # noqa: F811
) -> None:
    store = Store(load_config(populated).db)
    first = store.list_jobs()[0]
    store.close()
    capsys.readouterr()
    assert cli(populated, "show", str(first.id), "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {
        "schema_version", "id", "status", "company", "title", "url", "source", "location",
        "posted_at", "first_seen", "fit", "keyword_score", "summary", "red_flags", "description",
    }  # fmt: skip
    assert data["schema_version"] == SHOW_SCHEMA_VERSION
    assert data["id"] == first.id and data["title"] == first.title
    assert data["description"].strip(), "queued jobs must keep their posting text"


def test_show_text_and_unknown_id(
    populated: Path, capsys: pytest.CaptureFixture[str]  # noqa: F811
) -> None:
    capsys.readouterr()
    assert cli(populated, "show", "1") == 0
    assert "@" in capsys.readouterr().out
    assert cli(populated, "show", "9999") == 1
    assert "no job with id 9999" in capsys.readouterr().err


def test_description_survives_cache_pruning(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.sqlite3")
    job = Job("lever", "acme", "1", "Engineer", "Remote", "https://x/1", "Full text here")
    store.cache_jobs([job], NOW)
    assert store.add_if_new(job, 10, NOW)
    store.prune_cache(datetime(2027, 6, 1, tzinfo=timezone.utc), 14)
    assert store.cached_jobs() == []
    assert store.posting(1) == ("Full text here", None)
    assert store.posting(99) is None


def test_migration_backfills_description_from_cache(tmp_path: Path) -> None:
    db = tmp_path / "v3.sqlite3"
    store = Store(db)
    job = Job("lever", "acme", "1", "Engineer", "Remote", "https://x/1", "Backfill me",
              posted_at=NOW)  # fmt: skip
    other = Job("lever", "acme", "2", "Gone", "Remote", "https://x/2", "not cached")
    store.cache_jobs([job], NOW)
    store.add_if_new(job, 10, NOW)
    store.add_if_new(other, 10, NOW)
    store.close()
    # Rewind to v3: drop the new columns' data and the version, then reopen.
    conn = sqlite3.connect(db)
    conn.execute("UPDATE seen SET description = '', posted_at = NULL")
    conn.execute("PRAGMA user_version = 3")
    conn.execute("ALTER TABLE seen DROP COLUMN description")
    conn.execute("ALTER TABLE seen DROP COLUMN posted_at")
    conn.commit()
    conn.close()
    assert len(MIGRATIONS) == 4
    store = Store(db)
    assert store.posting(1) == ("Backfill me", NOW.isoformat())
    assert store.posting(2) == ("", None)  # no cache entry: nothing to recover, no crash
    store.close()
