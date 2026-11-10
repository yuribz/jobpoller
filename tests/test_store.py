from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from jobpoller.config import load_config
from jobpoller.migrations import MIGRATIONS, MigrationError, current_version
from jobpoller.models import Job, Ranking
from jobpoller.pipeline import run
from jobpoller.store import Store
from tests.fixtures import ats_handler, write_config
from tests.test_scoring import make

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
V0_SCHEMA = (
    "CREATE TABLE seen (key TEXT PRIMARY KEY, first_seen TEXT NOT NULL, company TEXT NOT NULL,"
    " title TEXT NOT NULL, url TEXT NOT NULL, score INTEGER NOT NULL)",
    "CREATE TABLE ranking (key TEXT NOT NULL, fingerprint TEXT NOT NULL, fit INTEGER NOT NULL,"
    " summary TEXT NOT NULL, red_flags TEXT NOT NULL, created TEXT NOT NULL,"
    " PRIMARY KEY (key, fingerprint))",
)


def job(n: int, **kw: object) -> Job:
    return make(job_id=str(n), title=f"Engineer {n}", **kw)


def test_fresh_db_is_latest_version(tmp_path: Path) -> None:
    db = tmp_path / "a.sqlite3"
    Store(db).close()
    conn = sqlite3.connect(db)
    assert current_version(conn) == len(MIGRATIONS)
    Store(db).close()  # reopening is a no-op
    assert current_version(conn) == len(MIGRATIONS)


def test_pre_framework_db_is_adopted_without_data_loss(tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(db)
    for stmt in V0_SCHEMA:
        conn.execute(stmt)
    conn.execute("INSERT INTO seen VALUES ('lever:acme:b', '2026-10-02', 'acme', 'B', 'u2', 7)")
    conn.execute(
        "INSERT INTO seen VALUES ('greenhouse:acme:a', '2026-10-01', 'acme', 'A', 'u1', 9)"
    )
    conn.execute(
        "INSERT INTO ranking VALUES ('lever:acme:b', 'fp', 77, 'ok', '[\"x\"]', '2026-10-02')"
    )
    conn.commit()
    conn.close()

    store = Store(db)
    rows = store.list_jobs()
    assert {r.key for r in rows} == {"lever:acme:b", "greenhouse:acme:a"}
    by_key = {r.key: r for r in rows}
    assert by_key["greenhouse:acme:a"].id == 1  # ids follow first-seen order
    assert by_key["lever:acme:b"].id == 2
    assert by_key["lever:acme:b"].fit == 77 and by_key["lever:acme:b"].red_flags == ("x",)
    assert by_key["lever:acme:b"].source == "lever" and by_key["lever:acme:b"].status == "new"
    assert by_key["greenhouse:acme:a"].fit is None
    store.close()


def test_failed_migration_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "x.sqlite3"

    def boom(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE half_done (x INTEGER)")
        raise RuntimeError("boom")

    monkeypatch.setattr("jobpoller.migrations.MIGRATIONS", [*MIGRATIONS, boom])
    with pytest.raises(RuntimeError):
        Store(db)
    conn = sqlite3.connect(db)
    assert current_version(conn) == len(MIGRATIONS)  # earlier migrations stuck, boom did not
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "half_done" not in tables


def test_newer_database_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "future.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(MigrationError):
        Store(db)


def test_status_changes_undo_and_listing(tmp_path: Path) -> None:
    store = Store(tmp_path / "s.sqlite3")
    for n, fit in [(1, 90), (2, 60), (3, 75)]:
        assert store.add_if_new(job(n), 10, NOW, Ranking(fit, f"s{n}", ("flag",)))
    assert [r.id for r in store.list_jobs()] == [1, 3, 2]  # best fit first
    assert [r.id for r in store.list_jobs(min_fit=70)] == [1, 3]

    later = NOW + timedelta(hours=1)
    assert store.set_status([1, 2, 99, 1], "promoted", later) == [1, 2]  # 99 unknown, 1 deduped
    assert store.set_status([1], "promoted", later) == []  # no-op
    assert [r.id for r in store.list_jobs(status="promoted")] == [1, 2]
    first = store.get(1)
    assert first is not None and first.status_updated == later.isoformat()

    store.set_status([3], "dismissed", later)
    assert store.undo_last(later) == [3]  # last batch only
    assert [r.status for r in store.list_jobs()] == ["promoted", "new", "promoted"]
    assert sorted(store.undo_last(later)) == [1, 2]  # the earlier two-job batch, together
    assert store.undo_last(later) == []
    with pytest.raises(ValueError):
        store.set_status([1], "bogus", later)


def test_cache_roundtrip_and_prune(tmp_path: Path) -> None:
    store = Store(tmp_path / "c.sqlite3")
    old, fresh = job(1), job(2, remote=True)
    store.cache_jobs([old], NOW - timedelta(days=20))
    store.cache_jobs([fresh], NOW - timedelta(days=2))
    assert store.prune_cache(NOW, 14) == 1
    assert store.cached_jobs() == [fresh]  # full Job survives, incl. posted_at and remote
    store.cache_jobs([fresh], NOW)  # refetch replaces, no duplicates
    assert len(store.cached_jobs()) == 1


def test_run_caches_everything_and_status_survives_reruns(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, llm=False))
    client = httpx.Client(transport=httpx.MockTransport(ats_handler))
    first = run(config, client, NOW)
    store = Store(config.db)
    cached_titles = {j.title for j in store.cached_jobs()}
    assert "Account Executive" in cached_titles  # filtered out, but still cached
    queued = store.list_jobs()
    assert len(queued) == len(first.new) and all(q.source for q in queued)

    store.set_status([q.id for q in queued], "dismissed", NOW)
    store.close()
    again = run(config, client, NOW + timedelta(days=1))
    assert again.new == []  # dismissed jobs never come back
    store = Store(config.db)
    assert {q.status for q in store.list_jobs()} == {"dismissed"}
    assert store.count() == len(first.new)


def test_cache_prunes_during_run(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, llm=False))
    store = Store(config.db)
    store.cache_jobs([job(7)], NOW - timedelta(days=config.cache_days + 1))
    store.close()
    run(config, httpx.Client(transport=httpx.MockTransport(ats_handler)), NOW)
    store = Store(config.db)
    assert all(j.job_id != "7" for j in store.cached_jobs())
