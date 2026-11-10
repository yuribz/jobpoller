from __future__ import annotations

import argparse
import re
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .bench import compare, format_report, run_bench
from .config import Config, ConfigError, load_config
from .digest import render_digest
from .models import STATUSES
from .notify import notify
from .pipeline import RunReport, collect, run
from .queuefile import QueueError, add_to_queue_file
from .ranker import LLMUnavailable, Ranker
from .scoring import score_job
from .sources import BoardError, fetch_board
from .store import QueuedJob, Store

_STATUS_COMMANDS = {"promote": "promoted", "dismiss": "dismissed", "applied": "applied"}


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
    now: datetime,
) -> int:
    resume_path = config.llm.resume_path
    if resume_path is None:
        raise ConfigError("bench needs llm.resume (path to your resume as .md/.txt)")
    resume = resume_path.read_text(encoding="utf-8")
    store = Store(config.db)
    try:
        pool = store.cached_jobs()
    finally:
        store.close()
    if not pool:  # nothing cached yet (never ran): fall back to a live fetch
        pool = collect(config, client, RunReport())
    scored = [(j, s) for j in pool if (s := score_job(j, config.criteria, now)) is not None]
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


# --- status workflow --------------------------------------------------------------------


def _queue_status(job: QueuedJob) -> str:
    label = "fit" if job.fit is not None else "kw"
    return f"discovered ({label} {job.sort_key})"


def _report_ids(store: Store, ids: list[int], changed: list[int], verb: str) -> int:
    """Print one line per requested id; returns 1 if any id does not exist."""
    bad = 0
    for job_id in dict.fromkeys(ids):
        job = store.get(job_id)
        if job is None:
            print(f"#{job_id}: no such id", file=sys.stderr)
            bad += 1
        elif job_id in changed:
            print(f"{verb} #{job_id} {job.title[:60]} @ {job.company}")
        else:
            print(f"#{job_id}: already {job.status}")
    return 1 if bad else 0


def _cmd_status(config: Config, cmd: str, ids: list[int], now: datetime) -> int:
    status = _STATUS_COMMANDS[cmd]
    if cmd == "promote" and config.queue_path is None:
        print("config error: promote needs queue_path in config.yaml", file=sys.stderr)
        return 2
    store = Store(config.db)
    try:
        changed = store.set_status(ids, status, now)
        if cmd == "promote" and config.queue_path is not None:
            try:
                for job_id in changed:
                    job = store.get(job_id)
                    if job is None:  # cannot happen: just updated
                        continue
                    number = add_to_queue_file(
                        config.queue_path, job.title, job.company, job.url, _queue_status(job)
                    )
                    note = f"queue row {number}" if number else "already in queue file"
                    print(f"promoted #{job_id} {job.title[:60]} @ {job.company} ({note})")
            except QueueError as exc:
                store.undo_last(now)  # keep DB and queue file consistent
                print(f"error: {exc}; status changes reverted", file=sys.stderr)
                return 1
            quiet = [i for i in ids if i not in changed]
            return _report_ids(store, quiet, [], "promoted")
        return _report_ids(store, ids, changed, status)
    finally:
        store.close()


def _cmd_list(config: Config, status: str | None, min_fit: int | None, verbose: bool) -> int:
    store = Store(config.db)
    try:
        rows = store.list_jobs(status, min_fit)
    finally:
        store.close()
    if not rows:
        print("no matching jobs")
        return 0
    for job in rows:
        label = f"fit {job.fit:3d}" if job.fit is not None else f"kw  {job.score:3d}"
        print(
            f"{job.id:>5}  {job.status:<9}  {label}  {job.title[:52]} @ {job.company[:24]}"
            f" ({job.source})"
        )
        if verbose:
            print(f"         {job.url}")
            if job.summary:
                print(f"         {job.summary}")
            if job.red_flags:
                print(f"         flags: {'; '.join(job.red_flags)}")
    return 0


def _since(value: str) -> timedelta:
    match = re.fullmatch(r"(\d+)([hdw])", value)
    if match is None:
        raise argparse.ArgumentTypeError("use a number plus h, d or w, e.g. 36h, 7d, 2w")
    unit = {"h": "hours", "d": "days", "w": "weeks"}[match.group(2)]
    return timedelta(**{unit: int(match.group(1))})


def _cmd_digest(
    config: Config, args: argparse.Namespace, now: datetime
) -> int:
    store = Store(config.db)
    try:
        jobs = store.list_jobs(min_fit=args.min_fit)
    finally:
        store.close()
    if args.since is not None:
        cutoff = (now - args.since).isoformat()
        jobs = [j for j in jobs if j.first_seen >= cutoff]
    path: Path = args.out or config.digest_path
    path.write_text(render_digest(jobs, now), encoding="utf-8")
    print(f"wrote {path} ({len(jobs)} jobs)")
    if args.open:
        webbrowser.open(path.resolve().as_uri())
    return 0


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser = argparse.ArgumentParser(prog="jobpoller", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", parents=[common], help="fetch, filter, score, queue new matches")
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="print matches without queuing them (LLM rankings are still cached)",
    )
    sub.add_parser("check", parents=[common], help="verify each board slug and the LLM respond")
    bench_p = sub.add_parser("bench", parents=[common], help="compare Ollama models")
    bench_p.add_argument("--models", required=True, help="comma-separated, e.g. qwen3:8b,qwen3:14b")
    bench_p.add_argument("-n", "--count", type=int, default=20, help="jobs to rank (default 20)")

    helps = {
        "promote": "add to your queue file and mark promoted",
        "dismiss": "mark dismissed (never shown again)",
        "applied": "mark applied",
    }
    for name, text in helps.items():
        p = sub.add_parser(name, parents=[common], help=text)
        p.add_argument("ids", type=int, nargs="+", help="ids from `jobpoller list`")

    list_p = sub.add_parser("list", parents=[common], help="show queued jobs, best first")
    list_p.add_argument("--status", choices=STATUSES)
    list_p.add_argument("--min-fit", type=int, help="LLM fit (keyword score if LLM was off)")
    list_p.add_argument("-v", "--verbose", action="store_true", help="show link and summary")

    dig = sub.add_parser("digest", parents=[common], help="write a static digest.html")
    dig.add_argument("--open", action="store_true", help="open it in your browser")
    dig.add_argument("--since", type=_since, help="only jobs first seen within e.g. 7d")
    dig.add_argument("--min-fit", type=int)
    dig.add_argument("--out", type=Path, help="output path (default: config `digest`)")
    return parser


def main(
    argv: list[str] | None = None,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
) -> int:
    args = _parser().parse_args(argv)
    now = now or datetime.now(timezone.utc)
    try:
        config = load_config(args.config)
    except (OSError, ConfigError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.cmd in _STATUS_COMMANDS:
        return _cmd_status(config, args.cmd, args.ids, now)
    if args.cmd == "list":
        return _cmd_list(config, args.status, args.min_fit, args.verbose)
    if args.cmd == "digest":
        return _cmd_digest(config, args, now)

    try:
        ranker = _build_ranker(config, transport)
    except (OSError, ConfigError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    warnings: list[str] = []
    try:
        with _client(config.user_agent, transport) as client:
            if args.cmd == "check":
                return _check(config, client, ranker)
            if args.cmd == "bench":
                models = [m.strip() for m in args.models.split(",") if m.strip()]
                if len(models) < 2 or args.count < 1:
                    print("bench needs at least two --models and -n >= 1", file=sys.stderr)
                    return 2
                try:
                    return _bench(config, client, models, args.count, transport, now)
                except (OSError, ConfigError) as exc:
                    print(f"config error: {exc}", file=sys.stderr)
                    return 2
            report = run(config, client, now, args.dry_run, ranker)
            if not args.dry_run:
                warnings = notify(config.notify, report.new, client)
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
    for err in [*report.errors, *warnings]:
        print(f"warn: {err}", file=sys.stderr)
    return 0
