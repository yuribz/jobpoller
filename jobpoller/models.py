from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Job:
    source: str  # greenhouse | lever | ashby | hn
    company: str
    job_id: str
    title: str
    location: str
    url: str
    description: str = ""
    remote: bool = False
    posted_at: datetime | None = None

    @property
    def key(self) -> str:
        """Stable identity used for dedupe across runs."""
        return f"{self.source}:{self.company.lower()}:{self.job_id}"


@dataclass(frozen=True)
class Ranking:
    """Result of the optional LLM fit assessment."""

    fit: int  # 0-100
    summary: str = ""
    red_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Match:
    job: Job
    score: int  # keyword score
    ranking: Ranking | None = None

    @property
    def sort_key(self) -> int:
        """LLM fit when available, otherwise the keyword score."""
        return self.ranking.fit if self.ranking else self.score
