import logging
import os
from pathlib import Path

import pytest

from jobpoller import logsetup
from jobpoller.logsetup import prune_old, setup_logging


@pytest.fixture(autouse=True)
def _reset_logger():
    yield
    logger = logging.getLogger("jobpoller")
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()


def test_writes_to_file_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "jobpoller.log"
    setup_logging(path)
    setup_logging(path)
    logging.getLogger("jobpoller.x").info("hello")
    for h in logging.getLogger("jobpoller").handlers:
        h.flush()
    assert path.read_text().count("hello") == 1


def test_size_is_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logsetup, "MAX_BYTES", 2000)
    path = tmp_path / "jobpoller.log"
    setup_logging(path)
    log = logging.getLogger("jobpoller.x")
    for i in range(500):
        log.info("line %d %s", i, "x" * 50)
    files = list(tmp_path.glob("jobpoller.log*"))
    assert len(files) == logsetup.BACKUPS + 1
    assert all(f.stat().st_size <= 2000 for f in files)


def test_prune_old_removes_only_stale_rotations(tmp_path: Path) -> None:
    active, fresh, stale, other = (
        tmp_path / n for n in ("a.log", "a.log.1", "a.log.2", "a.log.bak")
    )
    for f in (active, fresh, stale, other):
        f.write_text("x")
    old = 1_000_000.0
    for f in (active, stale, other):
        os.utime(f, (old, old))
    removed = prune_old(active, keep_days=30, now=old + 40 * 86400)
    assert removed == [stale]
    assert active.exists() and fresh.exists() and not stale.exists()
    assert other.exists()


def test_unwritable_path_does_not_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    assert setup_logging(blocker / "jobpoller.log") is None


def test_httpx_urls_not_logged(tmp_path: Path) -> None:
    path = tmp_path / "jobpoller.log"
    setup_logging(path)
    logging.getLogger("httpx").info("POST https://ntfy.sh/secret-topic")
    assert not path.exists() or "secret-topic" not in path.read_text()


def test_real_budget_is_under_10mb() -> None:
    assert (logsetup.BACKUPS + 1) * logsetup.MAX_BYTES < 10_000_000
