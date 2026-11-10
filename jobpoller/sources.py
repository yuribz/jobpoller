"""Fetchers for public, unauthenticated job-board endpoints.

Only documented JSON endpoints are used (Greenhouse Job Board API, Lever Postings API,
Ashby posting API, HN Algolia). No logins, no cookies, no HTML scraping of job sites.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx

from .models import Job


class BoardError(Exception):
    """A board could not be fetched or parsed (bad slug, outage, schema change)."""


_BLOCK_TAGS = re.compile(r"<(?:br|/?p|/li|/div|/h\d|/ul|/ol)\b[^>]*>", re.I)
_ANY_TAG = re.compile(r"<[^>]+>")


def strip_html(raw: str) -> str:
    """Turn an HTML fragment into readable plain text."""
    text = _BLOCK_TAGS.sub("\n", raw)
    text = _ANY_TAG.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _get_json(client: httpx.Client, url: str, params: dict[str, Any] | None = None) -> Any:
    try:
        response = client.get(url, params=params)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as exc:
        raise BoardError(f"{url}: {exc}") from exc
    except ValueError as exc:
        raise BoardError(f"{url}: invalid JSON ({exc})") from exc


def fetch_greenhouse(client: httpx.Client, slug: str) -> list[Job]:
    data = _get_json(
        client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", {"content": "true"}
    )
    jobs: list[Job] = []
    for item in data.get("jobs", []):
        if item.get("id") is None:
            continue
        location = (item.get("location") or {}).get("name", "") or ""
        # `content` is HTML-escaped HTML, so unescape once before stripping tags.
        description = strip_html(html.unescape(item.get("content") or ""))
        jobs.append(
            Job(
                source="greenhouse",
                company=slug,
                job_id=str(item["id"]),
                title=item.get("title", ""),
                location=location,
                url=item.get("absolute_url", ""),
                description=description,
                remote="remote" in location.lower(),
                posted_at=_parse_iso(item.get("first_published") or item.get("updated_at")),
            )
        )
    return jobs


def _join_locations(primary: Any, others: Any) -> str:
    """Primary location first, then any additional ones, de-duplicated, '; '-separated.

    Postings are often listed under one HQ but open in several places; filtering on the
    primary alone would hide, say, a San Diego opening listed under New York.
    """
    seen: list[str] = []
    candidates = [primary, *(others if isinstance(others, list) else [])]
    for cand in candidates:
        if isinstance(cand, dict):  # Ashby secondaryLocations entries
            cand = cand.get("location")
        if isinstance(cand, str) and cand.strip() and cand.strip() not in seen:
            seen.append(cand.strip())
    return "; ".join(seen)


def _lever_description(item: dict[str, Any]) -> str:
    """Intro + requirement lists + closing text.

    Lever puts responsibilities/qualifications in `lists`, outside `descriptionPlain`; in live
    data many postings name their stack only there, so skipping them under-scores real matches.
    """
    intro = item.get("descriptionPlain") or strip_html(item.get("description") or "")
    parts = [intro]
    for section in item.get("lists") or []:
        heading = strip_html(section.get("text") or "")
        body = strip_html(section.get("content") or "")
        if heading or body:
            parts.append(f"{heading}\n{body}".strip())
    parts.append(item.get("additionalPlain") or strip_html(item.get("additional") or ""))
    return "\n\n".join(p for p in parts if p)


def fetch_lever(client: httpx.Client, slug: str) -> list[Job]:
    data = _get_json(client, f"https://api.lever.co/v0/postings/{slug}", {"mode": "json"})
    if not isinstance(data, list):
        raise BoardError(f"lever/{slug}: expected a list, got {type(data).__name__}")
    jobs: list[Job] = []
    for item in data:
        if not item.get("id"):
            continue
        categories = item.get("categories") or {}
        location = _join_locations(categories.get("location"), categories.get("allLocations"))
        created = item.get("createdAt")
        posted = (
            datetime.fromtimestamp(created / 1000, tz=timezone.utc)
            if isinstance(created, (int, float))
            else None
        )
        description = _lever_description(item)
        jobs.append(
            Job(
                source="lever",
                company=slug,
                job_id=str(item["id"]),
                title=item.get("text", ""),
                location=location,
                url=item.get("hostedUrl") or item.get("applyUrl") or "",
                description=description,
                remote=item.get("workplaceType") == "remote" or "remote" in location.lower(),
                posted_at=posted,
            )
        )
    return jobs


def fetch_ashby(client: httpx.Client, slug: str) -> list[Job]:
    data = _get_json(
        client,
        f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
        {"includeCompensation": "true"},
    )
    jobs: list[Job] = []
    for item in data.get("jobs", []):
        if item.get("isListed") is False:
            continue
        job_id = item.get("id") or item.get("jobUrl")
        if not job_id:
            continue
        location = _join_locations(item.get("location"), item.get("secondaryLocations"))
        description = item.get("descriptionPlain") or strip_html(item.get("descriptionHtml") or "")
        # Live data sets isRemote=true on Hybrid roles; workplaceType is the reliable signal.
        workplace = item.get("workplaceType")
        remote = (
            workplace == "Remote" if isinstance(workplace, str) else bool(item.get("isRemote"))
        ) or "remote" in location.lower()
        jobs.append(
            Job(
                source="ashby",
                company=slug,
                job_id=str(job_id),
                title=item.get("title", ""),
                location=location,
                url=item.get("jobUrl") or item.get("applyUrl") or "",
                description=description,
                remote=remote,
                posted_at=_parse_iso(item.get("publishedAt")),
            )
        )
    return jobs


ALGOLIA = "https://hn.algolia.com/api/v1"


def fetch_hn_latest(client: httpx.Client) -> list[Job]:
    """Parse the newest 'Ask HN: Who is hiring?' thread via the Algolia HN API."""
    search = _get_json(
        client,
        f"{ALGOLIA}/search_by_date",
        {"query": "Ask HN: Who is hiring?", "tags": "story,author_whoishiring", "hitsPerPage": 5},
    )
    hits = [h for h in search.get("hits", []) if "who is hiring" in (h.get("title") or "").lower()]
    if not hits:
        return []
    story = hits[0]
    thread = _get_json(client, f"{ALGOLIA}/items/{story['objectID']}")
    posted = _parse_iso(story.get("created_at"))
    jobs: list[Job] = []
    for comment in thread.get("children", []):  # top-level comments are the postings
        raw = comment.get("text")
        if not raw or comment.get("author") is None:  # deleted / dead
            continue
        plain = strip_html(raw)
        header = plain.split("\n", 1)[0]
        parts = [p.strip() for p in header.split("|")]
        remainder = " | ".join(parts[1:])[:140] if len(parts) > 1 else header[:140]
        jobs.append(
            Job(
                source="hn",
                company=parts[0][:80] or "unknown",
                job_id=str(comment["id"]),
                title=remainder,
                location=remainder,
                url=f"https://news.ycombinator.com/item?id={comment['id']}",
                description=plain,
                remote="remote" in header.lower(),
                posted_at=posted,
            )
        )
    return jobs


FETCHERS: dict[str, Callable[[httpx.Client, str], list[Job]]] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


def fetch_board(client: httpx.Client, ats: str, slug: str) -> list[Job]:
    try:
        return FETCHERS[ats](client, slug)
    except (KeyError, TypeError, AttributeError) as exc:
        raise BoardError(f"{ats}/{slug}: unexpected response shape ({exc!r})") from exc
