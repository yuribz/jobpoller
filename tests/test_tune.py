from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from jobpoller.cli import main
from jobpoller.config import load_config
from jobpoller.pipeline import run
from jobpoller.scoring import Criteria, criteria_from_json, criteria_to_json
from jobpoller.store import Store
from jobpoller.tune import format_result, rule_key, tune
from tests.fixtures import ats_handler, write_config
from tests.test_scoring import NOW, crit, make


def jobs_for_tuning() -> list:  # type: ignore[type-arg]
    return [
        make(job_id="keep", title="Backend Engineer"),  # python+postgresql = 12: passes always
        make(job_id="weak", title="Software Engineer", description="We use Python."),  # 8
        make(job_id="staff", title="Staff Software Engineer"),  # excluded by title today
        make(job_id="sales", title="Account Executive"),  # never matches include_title
        make(job_id="far", title="Backend Engineer", location="Austin, TX"),
    ]


def test_diff_between_two_criteria() -> None:
    before = crit()  # min_score 10, excludes staff/intern
    after = crit(min_score=5, exclude_title=["intern", "backend"])
    result = tune(jobs_for_tuning(), after, before, NOW)

    assert (result.total, result.before_pass, result.after_pass) == (5, 1, 2)
    assert [r.job.job_id for r in result.newly_pass] == ["staff", "weak"]  # best score first
    weak = next(r for r in result.newly_pass if r.job.job_id == "weak")
    assert weak.before is not None and weak.before.reason == "score 8 < min 10"
    assert [r.job.job_id for r in result.newly_fail] == ["keep"]
    assert result.newly_fail[0].after.reason == "title:excluded 'backend'"
    assert result.rejections["title:no include match"] == 1
    # First failing rule wins: "far" dies on its title before the location rule is reached.
    assert result.rejections["title:excluded 'backend'"] == 2  # keep + far
    assert sum(result.rejections.values()) == result.total - result.after_pass


def test_without_baseline_there_is_no_diff() -> None:
    result = tune(jobs_for_tuning(), crit(), None, NOW)
    assert result.before_pass is None and result.newly_pass == [] and result.newly_fail == []
    text = format_result(result, set(), baseline_label=None)
    assert "No baseline" in text and "--against" in text


def test_rule_key_groups_detail() -> None:
    assert rule_key("location:no match 'Austin, TX'") == "location:no match"
    assert rule_key("age:61d > 45d") == "age:too old"
    assert rule_key("score 8 < min 12") == "score below min_score"
    assert rule_key("title:excluded 'staff'") == "title:excluded 'staff'"


def test_format_marks_jobs_already_in_inbox_and_limits_rows() -> None:
    many = [make(job_id=str(i), title="Backend Engineer") for i in range(30)]
    result = tune(many, crit(), crit(min_score=99), NOW)
    text = format_result(result, {many[0].key}, limit=5, baseline_label="test")
    assert "(showing 5 of 30)" in text and "was: score 12 < min 99" in text
    assert text.count("[in your inbox]") == 2  # shows once under newly passing, once under top
    assert "Top 5 under current settings" in text


def test_long_hn_style_company_is_clipped() -> None:
    job = make(company="Z" * 200, title="Backend Engineer")
    text = format_result(tune([job], crit(), None, NOW), set())
    assert "Z" * 200 not in text and ("Z" * 27 + "…") in text


def test_criteria_json_roundtrip_is_tolerant() -> None:
    c = Criteria(include_title=["a"], keywords={"python": 8}, max_age_days=None, min_score=3)
    assert criteria_from_json(criteria_to_json(c)) == c
    stored = json.loads(criteria_to_json(c))
    stored["removed_in_future"] = 1  # unknown keys are ignored
    del stored["accept_remote"]  # missing keys fall back to defaults
    assert criteria_from_json(json.dumps(stored)).accept_remote is True


# --- CLI / pipeline integration -------------------------------------------------------


@pytest.fixture
def transport() -> httpx.MockTransport:
    return httpx.MockTransport(ats_handler)


def cli(path: Path, *args: str, transport: httpx.BaseTransport) -> int:
    return main([*args, "--config", str(path)], transport=transport, now=NOW)


def test_real_runs_record_the_baseline_dry_runs_do_not(
    tmp_path: Path, transport: httpx.MockTransport
) -> None:
    config = load_config(write_config(tmp_path, llm=False))
    client = httpx.Client(transport=transport)
    run(config, client, NOW, dry_run=True)
    store = Store(config.db)
    assert store.get_meta("last_run_criteria") is None
    store.close()
    run(config, client, NOW)
    store = Store(config.db)
    data = json.loads(store.get_meta("last_run_criteria") or "{}")
    assert criteria_from_json(data["criteria"]) == config.criteria
    assert data["at"] == NOW.isoformat()


def test_cli_tune_diffs_against_last_run(
    tmp_path: Path, transport: httpx.MockTransport, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_config(tmp_path, llm=False)
    assert cli(path, "run", transport=transport) == 0
    capsys.readouterr()

    assert cli(path, "tune", transport=transport) == 0
    out = capsys.readouterr().out
    assert "Baseline: criteria from your last run" in out and "+0 newly pass, -0 newly fail" in out

    path.write_text(path.read_text().replace("min_score: 10", "min_score: 100"))
    assert cli(path, "tune", transport=transport) == 0
    out = capsys.readouterr().out
    assert "newly fail" in out and "now: score 12 < min 100" in out
    assert "[in your inbox]" in out  # those jobs are already queued from the real run
    assert "Nothing passes under current settings" in out
    assert "score below min_score" in out

    # --against compares with an explicit old config instead
    other = tmp_path / "other"
    other.mkdir()
    old = write_config(other, llm=False)  # an older copy with the original min_score
    assert cli(path, "tune", "--against", str(old), transport=transport) == 0
    assert f"Baseline: {old}" in capsys.readouterr().out
    assert cli(path, "tune", "--against", str(tmp_path / "nope.yaml"), transport=transport) == 2


def test_cli_tune_without_cache_or_baseline(
    tmp_path: Path, transport: httpx.MockTransport, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_config(tmp_path, llm=False)
    assert cli(path, "tune", transport=transport) == 1
    assert "cache is empty" in capsys.readouterr().out

    config = load_config(path)
    run(config, httpx.Client(transport=transport), NOW, dry_run=True)  # cache, but no baseline
    assert cli(path, "tune", transport=transport) == 0
    assert "No baseline" in capsys.readouterr().out
    store = Store(config.db)
    store.set_meta("last_run_criteria", "{not json")  # corrupt snapshot degrades gracefully
    store.close()
    assert cli(path, "tune", transport=transport) == 0
    assert "No baseline" in capsys.readouterr().out
