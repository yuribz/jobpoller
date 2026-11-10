from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from jobpoller.bench import ModelRun, compare, format_report
from jobpoller.cli import main
from tests.fixtures import ats_handler, write_config


def _run(model: str, fits: dict[str, int | None], latencies: list[float]) -> ModelRun:
    return ModelRun(model, fits, latencies, failures=sum(1 for f in fits.values() if f is None))


def test_compare_stats_agreement_and_disagreements() -> None:
    a = _run("a", {"j1": 80, "j2": 20, "j3": 50, "j4": None}, [9.0, 1.0, 2.0, 3.0])
    b = _run("b", {"j1": 70, "j2": 60, "j3": 50, "j4": 40}, [4.0, 1.0, 1.0, 1.0])
    result = compare([a, b], {"j1": "One", "j2": "Two"}, top=2)

    sa, sb = result.stats
    assert (sa.ok, sa.failures, sa.cold_s, sa.median_s, sa.p95_s) == (3, 1, 9.0, 2.0, 3.0)
    assert sb.mean_fit == pytest.approx(55.0) and sb.median_s == 1.0

    pair = result.pairs[0]
    assert pair.n == 3  # j4 failed on model a, so it is excluded from agreement
    assert pair.mean_abs_diff == pytest.approx((10 + 40 + 0) / 3)
    assert pair.within_10 == pytest.approx(2 / 3)
    assert pair.pearson is not None

    assert [d.key for d in result.disagreements] == ["j2", "j1"]
    assert result.disagreements[0].title == "Two" and result.disagreements[0].spread == 40


def test_compare_handles_degenerate_input() -> None:
    constant = _run("a", {"j1": 50, "j2": 50}, [1.0])
    other = _run("b", {"j1": 10, "j2": 90}, [])
    result = compare([constant, other], {})
    assert result.pairs[0].pearson is None  # constant series has no defined correlation
    assert result.stats[0].median_s is None  # only the cold call; no warm samples
    assert result.stats[1].cold_s is None
    empty = compare([_run("a", {"x": None}, []), _run("b", {"x": None}, [])], {})
    assert empty.pairs[0].n == 0 and empty.disagreements == []
    assert "n/a" in format_report(empty)


def _two_model_handler(calls: list[tuple[str, str]]) -> httpx.MockTransport:
    fits = {
        "qwen3:14b": {"Backend Software Engineer": 85, "Systems Software Engineer": 30},
        "qwen3:8b": {"Backend Software Engineer": 60, "Systems Software Engineer": 75},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "localhost":
            return ats_handler(request)
        path = request.url.path
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in fits]})
        body = json.loads(request.content)
        if path == "/api/generate":  # unload between models
            calls.append(("unload", body["model"]))
            assert body["keep_alive"] == 0
            return httpx.Response(200, json={})
        calls.append(("chat", body["model"]))
        prompt = body["messages"][-1]["content"]
        fit = next((f for t, f in fits[body["model"]].items() if f"Title: {t}" in prompt), 50)
        content = json.dumps({"fit": fit, "summary": "s", "red_flags": []})
        return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})

    return httpx.MockTransport(handler)


def test_cli_bench_ranks_same_jobs_with_each_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_config(tmp_path, llm=True)
    calls: list[tuple[str, str]] = []
    code = main(
        ["bench", "--config", str(path), "--models", "qwen3:14b,qwen3:8b", "-n", "3"],
        transport=_two_model_handler(calls),
    )
    out = capsys.readouterr().out
    assert code == 0
    chats = [m for kind, m in calls if kind == "chat"]
    assert chats == ["qwen3:14b"] * 3 + ["qwen3:8b"] * 3  # same 3 jobs, one model at a time
    assert calls.index(("unload", "qwen3:14b")) < calls.index(("chat", "qwen3:8b"))
    assert "qwen3:14b vs qwen3:8b" in out and "Biggest disagreements" in out
    assert "Systems Software Engineer" in out  # spread 45 is the largest disagreement


def test_cli_bench_reports_missing_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_config(tmp_path, llm=True)
    code = main(
        ["bench", "--config", str(path), "--models", "qwen3:14b,llama3:70b"],
        transport=_two_model_handler([]),
    )
    assert code == 1 and "ollama pull llama3:70b" in capsys.readouterr().err
    assert main(["bench", "--config", str(path), "--models", "only-one"]) == 2


def test_bench_uses_cached_jobs_without_fetching(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from jobpoller.config import load_config
    from jobpoller.pipeline import run
    from tests.test_store import NOW

    path = write_config(tmp_path, llm=True)
    ats = httpx.Client(transport=httpx.MockTransport(ats_handler))
    run(load_config(path), ats, NOW, dry_run=True)  # populates jobs_cache only

    inner = _two_model_handler([])

    def no_ats(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "localhost", f"unexpected fetch: {request.url}"
        return inner.handle_request(request)

    code = main(
        ["bench", "--config", str(path), "--models", "qwen3:14b,qwen3:8b", "-n", "2"],
        transport=httpx.MockTransport(no_ats),
    )
    assert code == 0 and "ranking 2 jobs" in capsys.readouterr().out
