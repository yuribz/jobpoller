"""File logging with a hard size cap and age-based pruning.

Total disk use is bounded by (BACKUPS + 1) * MAX_BYTES = 9 MB, under the 10 MB budget.
Only the `jobpoller` logger gets the handler, so httpx/httpcore (which log full request
URLs, including a private ntfy topic) never reach the file. Nothing is written to
stdout/stderr: `jobpoller mcp` speaks its protocol over stdout.
"""

from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

MAX_BYTES = 1_800_000
BACKUPS = 4
FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

_ROOT = "jobpoller"


def prune_old(path: Path, keep_days: int, now: float | None = None) -> list[Path]:
    """Delete rotated files (`<name>.1`, `.2`, ...) not modified within `keep_days`.

    The active log is never deleted; rotation bounds its size.
    """
    cutoff = (time.time() if now is None else now) - keep_days * 86400
    removed: list[Path] = []
    for old in path.parent.glob(path.name + ".*"):
        suffix = old.name[len(path.name) + 1 :]
        if not suffix.isdigit():
            continue
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
                removed.append(old)
        except OSError:
            continue  # raced with another run, or unreadable; pruning is best-effort
    return removed


def setup_logging(path: Path, keep_days: int = 30, level: int = logging.INFO) -> Path | None:
    """Attach the rotating file handler to the `jobpoller` logger. Idempotent.

    Returns the log path, or None if the file can't be opened: logging must never stop
    a job run.
    """
    logger = logging.getLogger(_ROOT)
    for handler in list(logger.handlers):
        if isinstance(handler, RotatingFileHandler):
            logger.removeHandler(handler)
            handler.close()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        prune_old(path, keep_days)
        handler = RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
        )
    except OSError:
        return None
    handler.setFormatter(logging.Formatter(FORMAT))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return path
