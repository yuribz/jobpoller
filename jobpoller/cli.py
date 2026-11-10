from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .bench import compare, format_report, run_bench
from .config import Config, ConfigError, load_config
from .pipeline import RunReport, collect, run
from .ranker import LLMUnavailable, Ranker
from .scoring import score_job
from .sources import BoardError, fetch_board
from .store import Store


def _client(user_agent: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=20.0,
        headers={"User-Agent": user_agent},
        follow_redirects=True,
        transport=transport,
    )


def _build_ranker(config: Config, transport: httpx.BaseTransport | None) -> Ranker | None:
    llm = config.llm
    if not llm.enabled:
        return None
    if llm.resume_path is None:
        raise ConfigError("llm.enabled requires llm.resume")
    resume = llm.resume_path.read_text(encoding="utf-8")
    if not resume.strip():
        raise ConfigError(f"resume file is empty: {llm.resume_path}")
    return Ranker(llm, resume, httpx.Client(timeout=llm.timeout_seconds, transport=transport))


def _check(config: Config, client: httpx.Client, ranker: Ranker | None) -> int:
    bad = 0
    targets = [(a, s) for a, slugs in config.boards.items() for s in slugs]
    for i, (ats, slug) in enumerate(targets):
        if i and config.delay_seconds:
            time.sleep(config.delay_seconds)
        try:
            print(f"ok    {ats}/{slug}: {len(fetch_board(client, ats, slug))} postings")
        except BoardError as exc:
            bad += 1
            print(f"FAIL  {ats}/{slug}: {exc}")
    if ranker is not None:
        try:
            print(f"ok    llm: {ranker.check()}")
        except LLMUnavailable as exc:
            bad += 1
            print(f"FAIL  llm: {exc}")
    return 1 if bad else 0


def _bench(
    config: Config,
    client: httpx.Client,
    models: list[str],
    count: int,
    transport: httpx.BaseTransport | None,
) -> int:
    resume_path = config.llm.resume_path
    if resume_path is None:
        raise ConfigError("bench needs llm.resume (path to your resume as .md/.txt)")
    resume = resume_path.read_text(encoding="utf-8")
    now = datetime.now(timezone.utc)
    store = Store(config.db)
    try:
        pool = store.cached_jobs()
    finally:
        store.close()
    if not pool:  # nothing cached yet (never ran): fall back to a live fetch
        pool = collect(config, client, RunReport())
    scored = [
        (j, s) for j in pool if (s := score_job(j, config.criteria, now)) is not None
    ]
    scored.sort(key=lambda p: (-p[1], p[0].key))  # deterministic: both models see the same jobs
    jobs = [j for j, _ in scored[:count]]
    if not jobs:
        print("no jobs matched your criteria; nothing to benchmark", file=sys.stderr)
        return 1
    print(f"ranking {len(jobs)} jobs with {', '.join(models)} (cold start included in 'cold')")
    try:
        runs = run_bench(
            config.llm,
            resume,
            jobs,
            models,
            lambda: httpx.Client(timeout=config.llm.timeout_seconds, transport=transport),
            log=print,
        )
    except LLMUnavailable as exc:
        print(f"FAIL  llm: {exc}", file=sys.stderr)
        return 1
    print()
    print(format_report(compare(runs, {j.key: f"{j.title} @ {j.company}" for j in jobs})))
    return 0


def main(argv: list[str] | None = None, transport: httpx.BaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobpoller", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="fetch, filter, score, append new matches")
    run_p.add_argument("--config", type=Path, default=Path("config.yaml"))
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="print matches without queuing them (LLM rankings are still cached)",
    )
    check_p = sub.add_parser("check", help="verify each board slug and the LLM respond")
    check_p.add_argument("--config", type=Path, default=Path("config.yaml"))
    bench_p = sub.add_parser("bench", help="compare Ollama models on the same jobs")
    bench_p.add_argument("--config", type=Path, default=Path("config.yaml"))
    bench_p.add_argument("--models", required=True, help="comma-separated, e.g. qwen3:8b,qwen3:14b")
    bench_p.add_argument("-n", "--count", type=int, default=20, help="jobs to rank (default 20)")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        ranker = _build_ranker(config, transport)
    except (OSError, ConfigError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    try:
        with _client(config.user_agent, transport) as client:
            if args.cmd == "check":
                return _check(config, client, ranker)
            if args.cmd == "bench":
                models = [m.strip() for m in args.models.split(",") if m.strip()]
                if len(models) < 2 or args.count < 1:
                    print("bench needs at least two --models and -n >= 1", file=sys.stderr)
                    return 2
                return _bench(config, client, models, args.count, transport)
            report = run(config, client, datetime.now(timezone.utc), args.dry_run, ranker)
    finally:
        if ranker is not None:
            ranker.close()

    verb = "would add" if args.dry_run else "added"
    summary = f"fetched {report.fetched}, matched {report.matched}"
    if ranker is not None:
        summary += f", llm-rejected {report.llm_rejected}, deferred {report.deferred}"
    print(f"{summary}, {verb} {len(report.new)} new")
    for m in sorted(report.new, key=lambda x: -x.sort_key)[:20]:
        fit = f"fit {m.ranking.fit:3d} " if m.ranking else ""
        print(f"  [{fit}kw {m.score:3d}] {m.job.title[:55]} @ {m.job.company} ({m.job.source})")
        print(f"      {m.job.url}")
        if m.ranking and m.ranking.summary:
            print(f"      {m.ranking.summary}")
    for err in report.errors:
        print(f"warn: {err}", file=sys.stderr)
    return 0
