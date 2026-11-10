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


def score_job(job: Job, criteria: Criteria, now: datetime) -> int | None:
    """Return a 0-100 score, or None if a hard filter rejects the job."""
    title = job.title
    if criteria.include_title and not any(has_term(t, title) for t in criteria.include_title):
        return None
    if any(has_term(t, title) for t in criteria.exclude_title):
        return None

    loc = job.location.lower()
    if any(x.lower() in loc for x in criteria.exclude_locations):
        return None
    if criteria.locations:
        in_place = any(x.lower() in loc for x in criteria.locations)
        if not (in_place or (criteria.accept_remote and job.remote)):
            return None

    body = f"{job.title}\n{job.description}"
    if any(has_term(t, body) for t in criteria.exclude_text):
        return None

    if criteria.max_age_days is not None and job.posted_at is not None:
        if now - job.posted_at > timedelta(days=criteria.max_age_days):
            return None

    score = 0
    for term, weight in criteria.keywords.items():
        if has_term(term, job.description):
            score += weight
        if has_term(term, job.title):
            score += weight  # title hits count double
    score = max(0, min(100, score))
    return score if score >= criteria.min_score else None
