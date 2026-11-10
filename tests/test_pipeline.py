from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from jobpoller.cli import main
from jobpoller.config import ConfigError, load_config
from jobpoller.pipeline import run
from jobpoller.sources import (
    BoardError,
    fetch_ashby,
    fetch_greenhouse,
    fetch_hn_latest,
    fetch_lever,
    strip_html,
)
from tests.fixtures import ats_handler, write_config

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def client() -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(ats_handler))


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    return write_config(tmp_path, llm=False)


def test_strip_html_handles_entities_and_breaks() -> None:
    assert strip_html("<p>A &amp; B<p>C<br>D") == "A & B\nC\nD"


def test_source_parsers(client: httpx.Client) -> None:
    gh = fetch_greenhouse(client, "goodco")
    assert gh[0].description == "We use Python & PostgreSQL."
    assert gh[0].posted_at is not None and gh[0].location == "San Diego, CA"

    lv = fetch_lever(client, "leverco")[0]
    assert lv.remote and lv.title == "Systems Software Engineer" and lv.posted_at is not None

    ab = fetch_ashby(client, "ashbyco")
    assert [j.job_id for j in ab] == ["x1"]  # unlisted job skipped
    assert ab[0].description == "Python, Docker, Linux."

    hn = fetch_hn_latest(client)
    assert [j.job_id for j in hn] == ["501", "503"]  # dead comment skipped
    assert hn[0].company == "Acme (YC S22)" and "San Diego" in hn[0].location


def test_bad_slug_raises_board_error(client: httpx.Client) -> None:
    with pytest.raises(BoardError):
        fetch_greenhouse(client, "nosuchco")


def test_full_run_is_idempotent_and_isolates_failures(
    cfg_path: Path, client: httpx.Client
) -> None:
    config = load_config(cfg_path)
    first = run(config, client, NOW)
    titles = sorted(m.job.title for m in first.new)
    assert "Backend Software Engineer" in titles  # greenhouse
    assert "Systems Software Engineer" in titles  # lever
    assert "Platform Engineer" in titles  # ashby
    assert any("Backend Software Engineer | San Diego" in t for t in titles)  # hn
    assert "Account Executive" not in titles
    assert not any("Marketing Manager" in t for t in titles)
    assert len(first.errors) == 1 and "nosuchco" in first.errors[0]

    rows = config.output.read_text().splitlines()
    assert sum(1 for r in rows if r.startswith("| 2026-11-09")) == len(first.new)

    second = run(config, client, NOW)
    assert second.new == [] and second.matched == first.matched
    assert len(config.output.read_text().splitlines()) == len(rows)


def test_dry_run_writes_nothing(cfg_path: Path, client: httpx.Client) -> None:
    config = load_config(cfg_path)
    report = run(config, client, NOW, dry_run=True)
    assert report.new
    assert not config.output.exists()
    assert run(config, client, NOW).new  # still new afterwards


def test_config_validation(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("boards:\n  workday: [x]\n")
    with pytest.raises(ConfigError):
        load_config(bad)
    no_resume = tmp_path / "noresume.yaml"
    no_resume.write_text("llm:\n  enabled: true\n")
    with pytest.raises(ConfigError):
        load_config(no_resume)


def test_cli_check_and_run(cfg_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    transport = httpx.MockTransport(ats_handler)
    assert main(["check", "--config", str(cfg_path)], transport=transport) == 1
    out = capsys.readouterr().out
    assert "ok    greenhouse/goodco: 2 postings" in out and "FAIL  greenhouse/nosuchco" in out

    assert main(["run", "--config", str(cfg_path)], transport=transport) == 0
    assert "added" in capsys.readouterr().out
    assert main(["run", "--config", "/nonexistent/config.yaml"]) == 2


def test_paths_expand_home_and_stay_relative_otherwise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "c.yaml"
    path.write_text(
        "queue_path: ~/jobs/queue.md\ndigest: out/d.html\ndb: /abs/x.sqlite3\n"
        "llm:\n  resume: ~/resume.md\n"
    )
    cfg = load_config(path)
    assert cfg.queue_path == tmp_path / "home" / "jobs" / "queue.md"
    assert cfg.llm.resume_path == tmp_path / "home" / "resume.md"
    assert cfg.digest_path == tmp_path / "out" / "d.html"  # relative: config folder
    assert cfg.db == Path("/abs/x.sqlite3")  # absolute stays absolute
