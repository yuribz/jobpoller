"""CLI workflow: run -> list -> promote/dismiss/applied -> digest, with notifications."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from jobpoller.cli import main
from jobpoller.config import ConfigError, load_config
from jobpoller.store import Store
from tests.fixtures import FakeOllama, ats_handler, write_config

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
FITS = {"Backend Software Engineer": 85, "Systems Software Engineer": 30, "Platform Engineer": 70}
QUEUE = """# Queue

| # | Role | Company | Apply link | Resume | Cover letter | Status |
|---|---|---|---|---|---|---|
| 1 | Hand written | Acme | https://acme/1 | a.pdf |  | applied |
"""


def setup(tmp_path: Path, extra: str = "", llm: bool = False) -> Path:
    path = write_config(tmp_path, llm=llm)
    path.write_text(path.read_text() + extra)
    return path


def cli(path: Path, *args: str, transport: httpx.BaseTransport | None = None) -> int:
    transport = transport or httpx.MockTransport(ats_handler)
    return main([*args, "--config", str(path)], transport=transport, now=NOW)


@pytest.fixture
def populated(tmp_path: Path) -> Path:
    (tmp_path / "queue.md").write_text(QUEUE)
    path = setup(tmp_path, "queue_path: queue.md\n")
    assert cli(path, "run") == 0
    return path


def test_list_shows_ids_status_and_filters(
    populated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert cli(populated, "list", "-v") == 0
    out = capsys.readouterr().out
    assert "Backend Software Engineer" in out and "new" in out and "https://" in out
    assert cli(populated, "list", "--status", "applied") == 0
    assert "no matching jobs" in capsys.readouterr().out
    assert cli(populated, "list", "--min-fit", "9999") == 0
    assert "no matching jobs" in capsys.readouterr().out


def test_promote_appends_rows_once_and_marks_status(
    populated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store(load_config(populated).db)
    first, second = [j.id for j in store.list_jobs()[:2]]
    capsys.readouterr()
    assert cli(populated, "promote", str(first), str(second)) == 0
    queue = (populated.parent / "queue.md").read_text()
    rows = [line for line in queue.splitlines() if "(kw " in line or "(fit " in line]
    assert len(rows) == 2 and rows[0].startswith("| 2 |") and rows[1].startswith("| 3 |")
    assert queue.startswith(QUEUE.rstrip("\n"))  # hand-written content untouched
    assert {store.get(i).status for i in (first, second)} == {"promoted"}  # type: ignore[union-attr]

    assert cli(populated, "promote", str(first)) == 0  # idempotent
    assert "already promoted" in capsys.readouterr().out
    assert (populated.parent / "queue.md").read_text() == queue

    assert cli(populated, "promote", "9999") == 1
    assert "no such id" in capsys.readouterr().err


def test_dismiss_and_applied_persist_across_runs(populated: Path) -> None:
    store = Store(load_config(populated).db)
    ids = [j.id for j in store.list_jobs()]
    assert cli(populated, "dismiss", str(ids[0])) == 0
    assert cli(populated, "applied", str(ids[1])) == 0
    assert cli(populated, "run") == 0
    got = {j.id: j.status for j in store.list_jobs()}
    assert got[ids[0]] == "dismissed" and got[ids[1]] == "applied"
    assert len(got) == len(ids)  # nothing resurfaced


def test_promote_needs_queue_path_and_rolls_back_on_write_failure(tmp_path: Path) -> None:
    path = setup(tmp_path)
    cli(path, "run")
    assert cli(path, "promote", "1") == 2  # queue_path not configured
    other = tmp_path / "other"
    other.mkdir()
    bad = setup(other, "queue_path: missing_dir/q.md\n")
    cli(bad, "run")
    assert cli(bad, "promote", "1") == 1
    store = Store(load_config(bad).db)
    assert store.get(1) is not None and store.get(1).status == "new"  # type: ignore[union-attr]


def test_digest_writes_filtered_file_and_opens_browser(
    populated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr("jobpoller.cli.webbrowser.open", lambda url: opened.append(url) or True)
    out = populated.parent / "d.html"
    assert cli(populated, "digest", "--out", str(out), "--open") == 0
    doc = out.read_text()
    assert doc.count("<article") == Store(load_config(populated).db).count()
    assert opened == [out.resolve().as_uri()]

    assert cli(populated, "digest", "--out", str(out), "--min-fit", "9999") == 0
    assert "<article" not in out.read_text()
    # 'run' stamped first_seen at NOW; a window that ends before that excludes everything
    later = main(["digest", "--config", str(populated), "--out", str(out), "--since", "1d"],
                 now=NOW + timedelta(days=3))
    assert later == 0 and "<article" not in out.read_text()
    default = load_config(populated).digest_path
    assert cli(populated, "digest") == 0 and default.exists()
    with pytest.raises(SystemExit):
        cli(populated, "digest", "--since", "soon")


def test_run_notifies_on_strong_match_only_when_real_and_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        sent.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr("jobpoller.notify.shutil.which", lambda _: "/usr/bin/notify-send")
    monkeypatch.setattr("jobpoller.notify.subprocess.run", fake_run)
    path = setup(tmp_path, "notify:\n  threshold: 80\n", llm=True)
    transport = httpx.MockTransport(FakeOllama(FITS))

    assert cli(path, "run", "--dry-run", transport=transport) == 0
    assert sent == []  # dry runs never notify
    assert cli(path, "run", transport=transport) == 0
    assert len(sent) == 1 and sent[0][2] == "jobpoller: 2 strong matches"  # two jobs at fit 85
    assert cli(path, "run", transport=transport) == 0
    assert len(sent) == 1  # nothing new, nothing sent


def test_notification_failure_never_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        raise OSError("no dbus")

    monkeypatch.setattr("jobpoller.notify.shutil.which", lambda _: "/usr/bin/notify-send")
    monkeypatch.setattr("jobpoller.notify.subprocess.run", boom)
    path = setup(tmp_path, llm=True)
    assert cli(path, "run", transport=httpx.MockTransport(FakeOllama(FITS))) == 0
    assert "notify-send failed" in capsys.readouterr().err


def test_bad_notify_config(tmp_path: Path) -> None:
    path = setup(tmp_path, "notify:\n  ntfy: not-a-url\n")
    with pytest.raises(ConfigError):
        load_config(path)
