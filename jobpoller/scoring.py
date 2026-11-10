from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache

from .models import Job


@dataclass
class Criteria:
    include_title: list[str] = field(default_factory=list)  # any must appear in the title
    exclude_title: list[str] = field(default_factory=list)  # none may appear in the title
    locations: list[str] = field(default_factory=list)  # substrings; empty = any location
    accept_remote: bool = True
    exclude_locations: list[str] = field(default_factory=list)
    exclude_text: list[str] = field(default_factory=list)  # hard-reject phrases in the posting
    keywords: dict[str, int] = field(default_factory=dict)  # weight per distinct hit (+/-)
    min_score: int = 10
    max_age_days: int | None = 45


@lru_cache(maxsize=512)
def _term_re(term: str) -> re.Pattern[str]:
    # Boundaries that treat + and # as word characters, so "java" != "javascript",
    # "c++" and "c#" match, and "intern" != "internal".
    return re.compile(rf"(?<![A-Za-z0-9+#]){re.escape(term.lower())}(?![A-Za-z0-9+#])")


def has_term(term: str, text: str) -> bool:
    return _term_re(term).search(text.lower()) is not None


@dataclass(frozen=True)
class Verdict:
    """Outcome of scoring one job: kept or rejected, and exactly why."""

    passed: bool
    score: int | None  # None if a hard filter rejected it before scoring
    reason: str  # "" when passed; otherwise e.g. "title:excluded 'staff'" or "score 8 < min 12"


def _reject(reason: str) -> Verdict:
    return Verdict(False, None, reason)


def explain_job(job: Job, criteria: Criteria, now: datetime) -> Verdict:
    """Apply every filter in order, reporting the first one that rejects the job."""
    title = job.title
    if criteria.include_title and not any(has_term(t, title) for t in criteria.include_title):
        return _reject("title:no include match")
    for t in criteria.exclude_title:
        if has_term(t, title):
            return _reject(f"title:excluded '{t}'")

    loc = job.location.lower()
    for x in criteria.exclude_locations:
        if x.lower() in loc:
            return _reject(f"location:excluded '{x}'")
    if criteria.locations:
        in_place = any(x.lower() in loc for x in criteria.locations)
        if not (in_place or (criteria.accept_remote and job.remote)):
            shown = job.location[:40] or "(none)"
            return _reject(f"location:no match '{shown}'")

    body = f"{job.title}\n{job.description}"
    for t in criteria.exclude_text:
        if has_term(t, body):
            return _reject(f"text:excluded '{t}'")

    if criteria.max_age_days is not None and job.posted_at is not None:
        age = now - job.posted_at
        if age > timedelta(days=criteria.max_age_days):
            return _reject(f"age:{age.days}d > {criteria.max_age_days}d")

    score = 0
    for term, weight in criteria.keywords.items():
        if has_term(term, job.description):
            score += weight
        if has_term(term, job.title):
            score += weight  # title hits count double
    score = max(0, min(100, score))
    if score < criteria.min_score:
        return Verdict(False, score, f"score {score} < min {criteria.min_score}")
    return Verdict(True, score, "")


def score_job(job: Job, criteria: Criteria, now: datetime) -> int | None:
    """Return a 0-100 score, or None if any filter rejects the job."""
    verdict = explain_job(job, criteria, now)
    return verdict.score if verdict.passed else None
