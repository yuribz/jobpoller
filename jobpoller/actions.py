"""Operations shared by the CLI, the MCP server and `chat`. No printing, no argument parsing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import httpx

from .config import Config, ConfigError
from .manual import Extracted, ManualError, build_job
from .queuefile import QueueError, add_to_queue_file
from .ranker import LLMUnavailable, Ranker, RankError
from .scoring import explain_job
from .store import QueuedJob, Store

__all__ = [
    "AddResult",
    "ManualError",
    "PromoteResult",
    "QueueError",
    "add_posting",
    "build_ranker",
    "promote_jobs",
    "queue_status",
]


def queue_status(job: QueuedJob) -> str:
    label = "fit" if job.fit is not None else "kw"
    return f"discovered ({label} {job.sort_key})"


@dataclass(frozen=True)
class PromoteResult:
    changed: list[int]  # ids whose status changed now
    notes: dict[int, str]  # id -> "queue row N" / "already in queue file"


def promote_jobs(config: Config, store: Store, ids: list[int], now: datetime) -> PromoteResult:
    """Mark jobs promoted and append them to the queue file.

    Raises QueueError (after reverting the status change) if the queue file can't be written,
    so the DB and the file never disagree. Raises QueueError if `queue_path` is unset.
    """
    if config.queue_path is None:
        raise QueueError("promote needs queue_path in config.yaml")
    changed = store.set_status(ids, "promoted", now)
    notes: dict[int, str] = {}
    try:
        for job_id in changed:
            job = store.get(job_id)
            if job is None:  # cannot happen: just updated
                continue
            number = add_to_queue_file(
                config.queue_path, job.title, job.company, job.url, queue_status(job)
            )
            notes[job_id] = f"queue row {number}" if number else "already in queue file"
    except QueueError:
        store.undo_last(now)
        raise
    return PromoteResult(changed, notes)


@dataclass(frozen=True)
class AddResult:
    job_id: int
    created: bool  # False: this URL was already queued, nothing changed
    title: str
    company: str
    fit: int | None
    keyword: int | None
    warnings: list[str] = field(default_factory=list)


def add_posting(
    config: Config,
    store: Store,
    ranker: Ranker | None,
    extracted: Extracted,
    now: datetime,
    *,
    company: str = "",
    title: str = "",
    location: str = "",
    url: str = "",
) -> AddResult:
    """Queue a posting the user supplied. Never fetches a page. Raises ManualError if unusable."""
    job, warnings = build_job(
        extracted, company=company, title=title, location=location, url=url
    )
    existing = store.find_by_url(job.url)
    if existing is not None:
        return AddResult(existing, False, job.title, job.company, None, None, warnings)

    ranking = None
    if ranker is not None:
        ranking = store.get_ranking(job, ranker.fingerprint)
        if ranking is None:
            try:
                ranking = ranker.rank(job)
                store.put_ranking(job, ranker.fingerprint, ranking, now)
            except (LLMUnavailable, RankError) as exc:
                warnings.append(f"not scored ({exc}); added without a fit")
    # You chose this job, so the keyword filters don't veto it; they only give a score.
    keyword = explain_job(job, config.criteria, now).score or 0
    store.add_if_new(job, keyword, now, ranking)
    found = store.find_by_url(job.url)
    if found is None:  # cannot happen: just inserted
        raise ManualError("could not save the job")
    return AddResult(
        found, True, job.title, job.company, ranking.fit if ranking else None, keyword, warnings
    )


def build_ranker(config: Config, transport: httpx.BaseTransport | None = None) -> Ranker | None:
    """The configured Ollama ranker, or None when llm.enabled is off. Caller closes it."""
    llm = config.llm
    if not llm.enabled:
        return None
    if llm.resume_path is None:
        raise ConfigError("llm.enabled requires llm.resume")
    resume = llm.resume_path.read_text(encoding="utf-8")
    if not resume.strip():
        raise ConfigError(f"resume file is empty: {llm.resume_path}")
    return Ranker(llm, resume, httpx.Client(timeout=llm.timeout_seconds, transport=transport))
