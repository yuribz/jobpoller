from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .actions import add_posting, build_ranker, promote_jobs
from .bench import compare, format_report, run_bench
from .chat import repl
from .config import PROFILES, Config, ConfigError, load_config
from .digest import render_digest
from .doctor import create_config_from_example, run_checks
from .listmd import render_list_md
from .logsetup import setup_logging
from .manual import ManualError, decode, extract, read_file
from .models import STATUSES
from .notify import notify
from .pipeline import RunReport, collect, run
from .queuefile import QueueError
from .ranker import LLMUnavailable, Ranker
from .schedule import ScheduleError, apply_install, normalize_time, plan_install, remove, status
from .scoring import criteria_from_json, score_job
from .sources import BoardError, fetch_board
from .store import Store
from .tools import ToolContext
from .tune import format_result, tune
from .views import job_payload

log = logging.getLogger(__name__)

_STATUS_COMMANDS = {"promote": "promoted", "dismiss": "dismissed", "applied": "applied"}


def _client(user_agent: str, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=20.0,
        headers={"User-Agent": user_agent},
        follow_redirects=True,
        transport=transport,
    )


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
        if cmd == "promote":
            try:
                result = promote_jobs(config, store, ids, now)
            except QueueError as exc:
                print(f"error: {exc}; status changes reverted", file=sys.stderr)
                return 1
            for job_id in result.changed:
                job = store.get(job_id)
                if job is not None:
                    print(
                        f"promoted #{job_id} {job.title[:60]} @ {job.company}"
                        f" ({result.notes.get(job_id, '')})"
                    )
            quiet = [i for i in ids if i not in result.changed]
            return _report_ids(store, quiet, [], "promoted")
        changed = store.set_status(ids, status, now)
        return _report_ids(store, ids, changed, status)
    finally:
        store.close()


def _cmd_list(
    config: Config, status: str | None, min_fit: int | None, verbose: bool, markdown: bool = False
) -> int:
    store = Store(config.db)
    try:
        rows = store.list_jobs(status, min_fit)
    finally:
        store.close()
    if not rows:
        print("no matching jobs")
        return 0
    if markdown:
        print(render_list_md(rows), end="")
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


def _cmd_add(
    config: Config, args: argparse.Namespace, ranker: Ranker | None, now: datetime
) -> int:
    """Add a posting you saved or pasted yourself (e.g. from LinkedIn). Never fetches a page."""
    store = Store(config.db)
    try:
        try:
            if args.file in (None, "-"):
                if args.file is None and sys.stdin.isatty():
                    raise ManualError("give a .txt/.md/.html file, or pipe the posting text in")
                extracted = extract(decode(sys.stdin.buffer.read()), args.format or "text")
            else:
                extracted = read_file(Path(args.file), args.format)
            result = add_posting(
                config,
                store,
                ranker,
                extracted,
                now,
                company=args.company or "",
                title=args.title or "",
                location=args.location or "",
                url=args.url or "",
            )
        except ManualError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    finally:
        store.close()
    for warning in result.warnings:
        print(f"warn: {warning}", file=sys.stderr)
    if not result.created:
        print(f"already queued as #{result.job_id} (nothing changed)")
    else:
        label = f"fit {result.fit}" if result.fit is not None else f"kw {result.keyword}"
        print(f"added #{result.job_id} {result.title[:60]} @ {result.company} ({label})")
        print(f"  jobpoller show {result.job_id}   |   jobpoller promote {result.job_id}")
    if args.promote:
        return _cmd_status(config, "promote", [result.job_id], now)
    return 0


def _cmd_mcp(config: Config, read_only: bool) -> int:
    try:
        from .mcp_server import serve
    except ImportError:
        print("error: MCP support is not installed: pip install 'jobpoller[mcp]'", file=sys.stderr)
        return 1
    return serve(config, read_only)


def _cmd_chat(
    config: Config,
    args: argparse.Namespace,
    transport: httpx.BaseTransport | None,
    now: datetime,
) -> int:
    ctx = ToolContext(
        config, transport=transport, read_only=args.read_only, origin="chat", now=lambda: now
    )
    return repl(config, ctx, model=args.model, assume_yes=args.yes, transport=transport)


def _cmd_show(config: Config, job_id: int, as_json: bool) -> int:
    store = Store(config.db)
    try:
        job = store.get(job_id)
        posting = store.posting(job_id)
    finally:
        store.close()
    if job is None or posting is None:
        print(f"error: no job with id {job_id} (see `jobpoller list`)", file=sys.stderr)
        return 1
    description, posted_at = posting
    if as_json:
        print(json.dumps(job_payload(job, description, posted_at), indent=2, ensure_ascii=False))
        return 0
    label = f"fit {job.fit}" if job.fit is not None else f"kw {job.score}"
    print(f"{job.title} @ {job.company}  [{job.status}, {label}]")
    print(f"{job.location or 'location unknown'}  |  {job.source}  |  {job.url}")
    if job.summary:
        print(f"\nAI summary: {job.summary}")
    if job.red_flags:
        print(f"Flags: {'; '.join(job.red_flags)}")
    if description:
        print(f"\n{description}")
    else:
        print("\n(no posting text stored for this job; open the link)")
    return 0


def _cmd_init(
    config_path: Path,
    assume_yes: bool,
    gpu: bool,
    transport: httpx.BaseTransport | None,
    profile: str | None = None,
) -> int:
    if not config_path.exists():
        print(f"No config at {config_path}.")
        answer = "y" if assume_yes else ""
        if not assume_yes and sys.stdin.isatty():
            answer = input("Create it from config.example.yaml? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            try:
                create_config_from_example(config_path, profile)
            except OSError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            print(f"Created {config_path}. Edit boards/criteria/llm, then run `jobpoller check`.")
            if profile in ("light", "balanced"):
                model = PROFILES[profile]["model"]
                print(f"Profile '{profile}': pull its model with `ollama pull {model}`.")
        else:
            print("Skipped. Copy config.example.yaml to config.yaml (or rerun with --yes).")
    print("\njobpoller setup check")
    failed = 0
    for result in run_checks(config_path, transport, gpu=gpu):
        print(f"{'ok' if result.ok else 'FAIL':<5} {result.name}: {result.detail}")
        if not result.ok:
            failed += 1
            if result.fix:
                print(f"      fix: {result.fix}")
    print("\nAll set." if not failed else f"\n{failed} item(s) need attention.")
    return 1 if failed else 0


def _cmd_schedule(args: argparse.Namespace) -> int:
    try:
        if args.action == "status":
            state = status()
            if not state.installed:
                print("not installed (run `jobpoller schedule install`)")
                return 1
            print(f"installed: yes  enabled: {state.enabled}  active: {state.active}")
            print(f"next run: {state.next_run}")
            print(f"last run: {state.last_result}")
            print("logs: journalctl --user -u jobpoller.service -n 50")
            if state.linger is False:
                print(
                    "note: lingering is off, so a run missed while logged out happens at "
                    "next login (enable with `loginctl enable-linger $USER`)"
                )
            return 0
        if args.action == "remove":
            print("removed jobpoller timer" if remove() else "nothing to remove")
            return 0
        config = load_config(args.config)
        time = normalize_time(args.time) if args.time else config.schedule_time
        plan = plan_install(Path(sys.executable), args.config.resolve(), time)
        print(f"Daily run at {time} using {sys.executable}\nThis will:")
        for line in plan.describe():
            print(f"  {line}")
        if args.dry_run:
            print(f"\n--- {plan.service_path.name}\n{plan.service_text}")
            print(f"--- {plan.timer_path.name}\n{plan.timer_text}(dry run: nothing changed)")
            return 0
        apply_install(plan)
        print("installed. Check with `jobpoller schedule status`.")
        return 0
    except (ScheduleError, ConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_tune(config: Config, args: argparse.Namespace, now: datetime) -> int:
    store = Store(config.db)
    try:
        jobs = store.cached_jobs()
        inbox = store.queued_keys()
        span = store.cache_range()
        saved = store.get_meta("last_run_criteria")
    finally:
        store.close()
    if not jobs or span is None:
        print("The cache is empty. Run `jobpoller run` first so there is something to re-score.")
        return 1

    before = None
    label: str | None = None
    if args.against is not None:
        try:
            before = load_config(args.against).criteria
        except (OSError, ConfigError) as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
        label = str(args.against)
    elif saved:
        try:
            data = json.loads(saved)
            before = criteria_from_json(data["criteria"])
            when = str(data["at"])[:16].replace("T", " ")
            label = f"criteria from your last run ({when} UTC)"
        except (ValueError, KeyError, TypeError):
            before = None  # unreadable snapshot: behave as if there is none

    print(
        f"Re-scored {len(jobs)} cached jobs (fetched {span[0][:10]} to {span[1][:10]}) "
        "with your current config."
    )
    result = tune(jobs, config.criteria, before, now)
    print(format_result(result, inbox, args.limit, label))
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
    list_p.add_argument("-m", "--markdown", action="store_true", help="output as Markdown")
    list_p.add_argument("-v", "--verbose", action="store_true", help="show link and summary")

    add_p = sub.add_parser(
        "add", parents=[common], help="add a posting you saved yourself (txt, md or html)"
    )
    add_p.add_argument("file", nargs="?", help=".txt/.md/.html file, or - / omitted for stdin")
    add_p.add_argument("--url", help="apply link (required unless the HTML contains it)")
    add_p.add_argument("--company", help="required unless the HTML contains it")
    add_p.add_argument("--title", help="required unless the HTML contains it")
    add_p.add_argument("--location")
    add_p.add_argument("--format", choices=["text", "html"], help="override the file extension")
    add_p.add_argument("--no-score", action="store_true", help="skip the AI fit score")
    add_p.add_argument("--promote", action="store_true", help="also add it to your queue file")

    show_p = sub.add_parser("show", parents=[common], help="print one job with its full posting")
    show_p.add_argument("id", type=int, help="id from `jobpoller list`")
    show_p.add_argument("--json", action="store_true", help="machine-readable (for other tools)")

    mcp_p = sub.add_parser(
        "mcp", parents=[common], help="serve triage tools over MCP (stdio), e.g. for Claude Desktop"
    )
    mcp_p.add_argument("--read-only", action="store_true", help="only expose list/get tools")

    chat_p = sub.add_parser(
        "chat", parents=[common], help="triage the queue by chatting with a local Ollama model"
    )
    chat_p.add_argument("--model", help="override llm.model for this session")
    chat_p.add_argument("--read-only", action="store_true", help="only expose list/get tools")
    chat_p.add_argument(
        "--yes", action="store_true", help="don't ask before dismiss/promote/add_posting"
    )

    dig = sub.add_parser("digest", parents=[common], help="write a static digest.html")
    dig.add_argument("--open", action="store_true", help="open it in your browser")
    dig.add_argument("--since", type=_since, help="only jobs first seen within e.g. 7d")
    dig.add_argument("--min-fit", type=int)
    dig.add_argument("--out", type=Path, help="output path (default: config `digest`)")
    tune_p = sub.add_parser(
        "tune", parents=[common], help="re-score cached jobs with your edited criteria"
    )
    tune_p.add_argument("--against", type=Path, help="compare with this config instead of last run")
    tune_p.add_argument("--limit", type=int, default=15, help="rows per section (default 15)")

    init_p = sub.add_parser("init", parents=[common], help="guided setup checklist")
    init_p.add_argument("--yes", action="store_true", help="create config.yaml without asking")
    init_p.add_argument("--no-gpu", action="store_true", help="skip loading the model to check GPU")
    init_p.add_argument(
        "--profile",
        choices=sorted(PROFILES),
        help="hardware preset for a newly created config (light = no discrete GPU)",
    )

    sched = sub.add_parser("schedule", parents=[common], help="daily systemd user timer")
    sched.add_argument("action", choices=["install", "remove", "status"])
    sched.add_argument("--time", help="HH:MM (default: schedule_time in config, else 08:00)")
    sched.add_argument("--dry-run", action="store_true", help="show what would change")
    return parser


def main(
    argv: list[str] | None = None,
    transport: httpx.BaseTransport | None = None,
    now: datetime | None = None,
) -> int:
    args = _parser().parse_args(argv)
    now = now or datetime.now(timezone.utc)
    if args.cmd == "init":
        return _cmd_init(args.config, args.yes, not args.no_gpu, transport, args.profile)
    if args.cmd == "schedule":
        return _cmd_schedule(args)
    try:
        config = load_config(args.config)
    except (OSError, ConfigError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    setup_logging(config.log_path, config.log_days)
    log.info("start: %s", " ".join(argv if argv is not None else sys.argv[1:]))
    code = _dispatch(config, args, transport, now)
    log.info("exit %d: %s", code, args.cmd)
    return code


def _dispatch(
    config: Config,
    args: argparse.Namespace,
    transport: httpx.BaseTransport | None,
    now: datetime,
) -> int:
    if args.cmd in _STATUS_COMMANDS:
        return _cmd_status(config, args.cmd, args.ids, now)
    if args.cmd == "list":
        return _cmd_list(config, args.status, args.min_fit, args.verbose, args.markdown)
    if args.cmd == "show":
        return _cmd_show(config, args.id, args.json)
    if args.cmd == "digest":
        return _cmd_digest(config, args, now)
    if args.cmd == "mcp":
        return _cmd_mcp(config, args.read_only)
    if args.cmd == "chat":
        return _cmd_chat(config, args, transport, now)
    if args.cmd == "tune":
        return _cmd_tune(config, args, now)

    try:
        skip_ranker = args.cmd == "add" and args.no_score
        ranker = None if skip_ranker else build_ranker(config, transport)
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
            if args.cmd == "add":
                return _cmd_add(config, args, ranker, now)
            report = run(config, client, now, args.dry_run, ranker)
            if not args.dry_run:
                status = (
                    f"fetched {report.fetched}, matched {report.matched}, "
                    f"added {len(report.new)}"
                )
                if report.errors:
                    status += f"; {len(report.errors)} source errors"
                warnings = notify(config.notify, report.new, client, status=status)
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
    log.info(
        "run done: fetched=%d matched=%d added=%d llm_rejected=%d deferred=%d dry_run=%s",
        report.fetched, report.matched, len(report.new), report.llm_rejected,
        report.deferred, args.dry_run,
    )
    for err in [*report.errors, *warnings]:
        log.warning("%s", err)
        print(f"warn: {err}", file=sys.stderr)
    return 0
