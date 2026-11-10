"""Parser tests against trimmed, sanitized captures of real API responses (Nov 2026).

The fixtures in tests/fixtures/live/ are real payload shapes: if a vendor changes a field,
refresh them and these tests show what broke. Synthetic data in fixtures.py can't catch that.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from jobpoller.models import Job
from jobpoller.sources import fetch_ashby, fetch_greenhouse, fetch_hn_latest, fetch_lever

LIVE = Path(__file__).parent / "fixtures" / "live"


def _load(name: str) -> object:
    return json.loads((LIVE / f"{name}.json").read_text(encoding="utf-8"))


def _handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if host == "boards-api.greenhouse.io":
        return httpx.Response(200, json=_load("greenhouse"))
    if host == "api.lever.co":
        return httpx.Response(200, json=_load("lever"))
    if host == "api.ashbyhq.com":
        return httpx.Response(200, json=_load("ashby"))
    if host == "hn.algolia.com" and path.endswith("/search_by_date"):
        return httpx.Response(200, json=_load("hn_search"))
    if host == "hn.algolia.com" and "/items/" in path:
        return httpx.Response(200, json=_load("hn_thread"))
    return httpx.Response(404)


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(_handler))


def _assert_plain_text(job: Job) -> None:
    for junk in ("&lt;", "&gt;", "&nbsp;", "&amp;amp;", "<p>", "<li>", "<div", "<span"):
        assert junk not in job.description, f"{junk!r} leaked into {job.key}"


def test_greenhouse_live_shape(client: httpx.Client) -> None:
    jobs = fetch_greenhouse(client, "natera")
    assert len(jobs) == 3
    for job in jobs:
        assert job.title and job.url.startswith("https://") and job.description
        assert job.posted_at is not None and job.posted_at.tzinfo is not None
        _assert_plain_text(job)
    assert any("San Diego" in j.location for j in jobs)


def test_lever_live_shape(client: httpx.Client) -> None:
    jobs = fetch_lever(client, "zoox")
    assert len(jobs) == 3
    for job in jobs:
        assert job.description and job.posted_at is not None and job.url.startswith("https://")
        _assert_plain_text(job)
        # Requirements live in `lists`, not descriptionPlain; scoring needs them.
        assert "Qualifications" in job.description


def test_lever_includes_every_listed_location(client: httpx.Client) -> None:
    multi = [j for j in fetch_lever(client, "zoox") if ";" in j.location]
    assert multi, "fixture should contain a posting with several allLocations"


def test_ashby_hybrid_is_not_remote(client: httpx.Client) -> None:
    # Live Ashby sets isRemote=true on hybrid roles; workplaceType is the reliable signal.
    by_id = {j.job_id: j for j in fetch_ashby(client, "ramp")}
    raw = _load("ashby")
    assert isinstance(raw, dict)
    seen_hybrid_flagged_remote = False
    for item in raw["jobs"]:
        job = by_id[item["id"]]
        _assert_plain_text(job)
        if item["workplaceType"] == "Remote":
            assert job.remote
        elif "remote" not in job.location.lower():
            assert not job.remote, item["title"]
            seen_hybrid_flagged_remote |= bool(item["isRemote"])
    assert seen_hybrid_flagged_remote, "fixture should contain a hybrid role with isRemote=true"


def test_ashby_secondary_locations_are_searchable(client: httpx.Client) -> None:
    job = next(j for j in fetch_ashby(client, "ramp") if "Remote (US)" in j.location)
    assert ";" in job.location and job.location.startswith("New York")
    assert job.remote  # an explicit "Remote (US)" opening counts as remote


def test_hn_live_shape(client: httpx.Client) -> None:
    jobs = fetch_hn_latest(client)
    assert len(jobs) == 3  # the deleted comment is skipped
    for job in jobs:
        assert job.company and job.title and job.url.startswith("https://news.ycombinator.com/item")
        assert job.posted_at is not None and job.posted_at.year == 2026
        _assert_plain_text(job)
