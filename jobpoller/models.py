from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

STATUSES = ("new", "promoted", "applied", "dismissed")


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

    def to_json(self) -> str:
        return json.dumps(
            {
                "source": self.source,
                "company": self.company,
                "job_id": self.job_id,
                "title": self.title,
                "location": self.location,
                "url": self.url,
                "description": self.description,
                "remote": self.remote,
                "posted_at": self.posted_at.isoformat() if self.posted_at else None,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> Job:
        d = json.loads(raw)
        posted = d.get("posted_at")
        return cls(
            source=d["source"],
            company=d["company"],
            job_id=d["job_id"],
            title=d["title"],
            location=d["location"],
            url=d["url"],
            description=d.get("description", ""),
            remote=bool(d.get("remote", False)),
            posted_at=datetime.fromisoformat(posted) if posted else None,
        )


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
