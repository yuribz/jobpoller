from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from .config import Config
from .models import Job, Match
from .output import append_discovered
from .ranker import LLMUnavailable, Ranker, RankError
from .scoring import score_job
from .sources import BoardError, fetch_board, fetch_hn_latest
from .store import Store


@dataclass
class RunReport:
    fetched: int = 0
    matched: int = 0  # passed keyword filters
    new: list[Match] = field(default_factory=list)
    llm_rejected: int = 0  # ranked below llm.min_fit
    deferred: int = 0  # not ranked this run (budget spent or LLM down); retried next run
    errors: list[str] = field(default_factory=list)


def collect(config: Config, client: httpx.Client, report: RunReport) -> list[Job]:
    """Fetch every configured source; one failing board never aborts the run."""
    jobs: list[Job] = []
    targets: list[tuple[str, str]] = [
        (ats, slug) for ats, slugs in config.boards.items() for slug in slugs
    ]
    for i, (ats, slug) in enumerate(targets):
        if i and config.delay_seconds:
            time.sleep(config.delay_seconds)
        try:
            jobs.extend(fetch_board(client, ats, slug))
        except BoardError as exc:
            report.errors.append(str(exc))
    if config.hn:
        try:
            jobs.extend(fetch_hn_latest(client))
        except (BoardError, KeyError, TypeError) as exc:
            report.errors.append(f"hn: {exc}")
    return jobs


def run(
    config: Config,
    client: httpx.Client,
    now: datetime,
    dry_run: bool = False,
    ranker: Ranker | None = None,
) -> RunReport:
    """Fetch, keyword-filter, optionally LLM-rank, and queue new matches.

    With a ranker, a job is only queued once it has been ranked. If the LLM is unreachable,
    remaining jobs are deferred (not queued unranked), so nothing is lost: the next run
    picks them up. Set llm.enabled: false to run keyword-only.
    """
    report = RunReport()
    jobs = collect(config, client, report)
    report.fetched = len(jobs)

    candidates: list[tuple[Job, int]] = []
    for job in jobs:
        score = score_job(job, config.criteria, now)
        if score is not None:
            candidates.append((job, score))
    report.matched = len(candidates)
    candidates.sort(key=lambda c: -c[1])  # best keyword matches get ranked first

    llm = config.llm
    budget = llm.max_new_rankings
    llm_up = True
    store = Store(config.db)
    try:
        for job, score in candidates:
            if store.has(job):
                continue
            ranking = None
            if ranker is not None:
                if not llm_up:
                    report.deferred += 1
                    continue
                ranking = store.get_ranking(job, ranker.fingerprint)
                if ranking is None:
                    if budget <= 0:
                        report.deferred += 1
                        continue
                    budget -= 1
                    try:
                        ranking = ranker.rank(job)
                    except LLMUnavailable as exc:
                        report.errors.append(f"llm: {exc}; deferring remaining jobs")
                        llm_up = False
                        report.deferred += 1
                        continue
                    except RankError as exc:
                        report.errors.append(f"rank {job.key}: {exc}")
                        report.deferred += 1
                        continue
                    store.put_ranking(job, ranker.fingerprint, ranking, now)
                if ranking.fit < llm.min_fit:
                    report.llm_rejected += 1
                    continue
            if dry_run:
                report.new.append(Match(job, score, ranking))
            elif store.add_if_new(job, score, now):
                report.new.append(Match(job, score, ranking))
    finally:
        store.close()

    if not dry_run:
        append_discovered(config.output, report.new, now.date())
    return report
