from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from jobpoller.cli import main
from jobpoller.config import Config, load_config
from jobpoller.pipeline import run
from jobpoller.ranker import Ranker, RankError, parse_ranking
from tests.fixtures import FakeOllama, write_config

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
FITS = {"Backend Software Engineer": 85, "Systems Software Engineer": 30, "Platform Engineer": 70}


def setup(
    tmp_path: Path, fake: FakeOllama, extra_llm: str = ""
) -> tuple[Config, Ranker, httpx.Client]:
    config = load_config(write_config(tmp_path, llm=True, extra_llm=extra_llm))
    transport = httpx.MockTransport(fake)
    assert config.llm.resume_path is not None
    resume = config.llm.resume_path.read_text()
    ranker = Ranker(config.llm, resume, httpx.Client(transport=transport))
    return config, ranker, httpx.Client(transport=transport)


def test_parse_ranking_variants() -> None:
    raw = '<think>hmm</think>\n```json\n{"fit": 72, "summary": "ok", "red_flags": ["x"]}\n```'
    r = parse_ranking(raw)
    assert (r.fit, r.summary, r.red_flags) == (72, "ok", ("x",))
    assert parse_ranking('{"fit": 150, "summary": "", "red_flags": []}').fit == 100
    assert parse_ranking('{"fit": -5, "summary": "", "red_flags": []}').fit == 0
    for bad in ["no json here", '{"fit": "high"}', '{"fit": true}', "{broken", "[1, 2]"]:
        with pytest.raises(RankError):
            parse_ranking(bad)


def test_rank_filters_orders_and_caches(tmp_path: Path) -> None:
    fake = FakeOllama(FITS)
    config, ranker, client = setup(tmp_path, fake)
    report = run(config, client, NOW, ranker=ranker)

    titles = {m.job.title for m in report.new}
    assert "Systems Software Engineer" not in titles  # fit 30 < min_fit 50
    assert report.llm_rejected == 1 and len(fake.prompts) == 4
    fits = sorted((m.ranking.fit for m in report.new if m.ranking), reverse=True)
    assert fits == [85, 85, 70]

    payload = fake.payloads[0]
    assert payload["think"] is False and payload["stream"] is False
    assert payload["options"] == {"temperature": 0, "num_ctx": 8192}
    assert "Yuri. Python, Java, Rust" in fake.prompts[0]  # resume is in the prompt

    out = config.output.read_text()
    first_row = [r for r in out.splitlines() if r.startswith("| 2026-11-09")][0]
    assert "| 85 |" in first_row and "fit 85" in first_row  # best fit listed first

    second = run(config, client, NOW, ranker=ranker)
    assert second.new == [] and len(fake.prompts) == 4  # no new LLM calls (cache + seen)
    assert second.llm_rejected == 1  # rejection served from cache


def test_changing_model_reranks(tmp_path: Path) -> None:
    fake = FakeOllama(FITS, models=["qwen3:14b", "qwen3:8b"])
    config, ranker, client = setup(tmp_path, fake)
    run(config, client, NOW, ranker=ranker, dry_run=True)
    calls = len(fake.prompts)
    assert config.llm.resume_path is not None
    other = Ranker(
        replace(config.llm, model="qwen3:8b"),
        config.llm.resume_path.read_text(),
        httpx.Client(transport=httpx.MockTransport(fake)),
    )
    assert other.fingerprint != ranker.fingerprint
    run(config, client, NOW, ranker=other, dry_run=True)
    assert len(fake.prompts) == calls * 2  # different model -> cache miss -> re-rank


def test_llm_down_defers_then_recovers(tmp_path: Path) -> None:
    fake = FakeOllama(FITS, mode="down")
    config, ranker, client = setup(tmp_path, fake)
    report = run(config, client, NOW, ranker=ranker)
    assert report.new == [] and report.deferred == 4
    assert any(e.startswith("llm:") for e in report.errors)
    assert not config.output.exists()

    fake.mode = "ok"
    recovered = run(config, client, NOW, ranker=ranker)
    assert len(recovered.new) == 3 and recovered.deferred == 0


def test_bad_model_output_skips_job_and_retries_later(tmp_path: Path) -> None:
    fake = FakeOllama(FITS, mode="badjson")
    config, ranker, client = setup(tmp_path, fake)
    report = run(config, client, NOW, ranker=ranker)
    assert report.new == [] and report.deferred == 4
    assert sum(e.startswith("rank ") for e in report.errors) == 4


def test_ranking_budget_defers_rest(tmp_path: Path) -> None:
    fake = FakeOllama(FITS)
    config, ranker, client = setup(tmp_path, fake, extra_llm="  max_new_rankings: 1\n")
    report = run(config, client, NOW, ranker=ranker)
    assert len(fake.prompts) == 1 and report.deferred == 3
    # Highest keyword score (the Lever job, fit 30) was ranked first and filtered.
    assert "Systems Software Engineer" in fake.prompts[0]
    follow_up = run(config, client, NOW, ranker=ranker)
    assert len(fake.prompts) == 2 and follow_up.deferred == 2  # one more per run


def test_think_block_is_stripped(tmp_path: Path) -> None:
    fake = FakeOllama(FITS, mode="think")
    config, ranker, client = setup(tmp_path, fake)
    report = run(config, client, NOW, ranker=ranker)
    assert len(report.new) == 3


def test_cli_check_reports_llm(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write_config(tmp_path, llm=True)
    main(["check", "--config", str(path)], transport=httpx.MockTransport(FakeOllama(FITS)))
    assert "ok    llm: qwen3:14b ready" in capsys.readouterr().out

    missing = FakeOllama(FITS, models=["llama3:8b"])
    assert main(["check", "--config", str(path)], transport=httpx.MockTransport(missing)) == 1
    assert "ollama pull qwen3:14b" in capsys.readouterr().out


def test_cli_run_with_llm(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write_config(tmp_path, llm=True)
    code = main(["run", "--config", str(path)], transport=httpx.MockTransport(FakeOllama(FITS)))
    out = capsys.readouterr().out
    assert code == 0 and "llm-rejected 1" in out and "fit  85" in out


def test_missing_resume_is_config_error(tmp_path: Path) -> None:
    path = write_config(tmp_path, llm=True)
    (tmp_path / "resume.md").unlink()
    assert main(["run", "--config", str(path)]) == 2
