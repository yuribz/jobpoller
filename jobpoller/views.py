"""Plain-dict views of queued jobs, shared by `show --json`, the MCP server and `chat`."""

from __future__ import annotations

from .store import QueuedJob

SHOW_SCHEMA_VERSION = 1  # bump on any breaking change to `show --json` (companion tools read it)


def job_row(job: QueuedJob) -> dict[str, object]:
    """One list row: everything but the (large) posting text."""
    return {
        "id": job.id,
        "status": job.status,
        "company": job.company,
        "title": job.title,
        "location": job.location,
        "source": job.source,
        "url": job.url,
        "fit": job.fit,
        "keyword_score": job.score,
        "summary": job.summary,
        "red_flags": list(job.red_flags),
        "first_seen": job.first_seen,
    }


def job_payload(job: QueuedJob, description: str, posted_at: str | None) -> dict[str, object]:
    """The full `show --json` contract: a row plus the posting text and schema version."""
    return {
        "schema_version": SHOW_SCHEMA_VERSION,
        "id": job.id,
        "status": job.status,
        "company": job.company,
        "title": job.title,
        "url": job.url,
        "source": job.source,
        "location": job.location,
        "posted_at": posted_at,
        "first_seen": job.first_seen,
        "fit": job.fit,
        "keyword_score": job.score,
        "summary": job.summary,
        "red_flags": list(job.red_flags),
        "description": description,
    }
