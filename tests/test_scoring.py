from datetime import datetime, timedelta, timezone

from jobpoller.models import Job
from jobpoller.scoring import Criteria, has_term, score_job

NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


def make(**kw: object) -> Job:
    base: dict[str, object] = dict(
        source="greenhouse", company="acme", job_id="1", title="Backend Software Engineer",
        location="San Diego, CA", url="https://x/1", description="We use Python and PostgreSQL.",
        remote=False, posted_at=NOW - timedelta(days=3),
    )
    base.update(kw)
    return Job(**base)  # type: ignore[arg-type]


def crit(**kw: object) -> Criteria:
    base: dict[str, object] = dict(
        include_title=["software", "backend"], exclude_title=["staff", "intern"],
        locations=["san diego", "remote"], keywords={"python": 8, "postgresql": 4, "java": 8},
        min_score=10,
    )
    base.update(kw)
    return Criteria(**base)  # type: ignore[arg-type]


def test_term_boundaries() -> None:
    assert has_term("java", "Java developer")
    assert not has_term("java", "JavaScript developer")
    assert has_term("c++", "strong C++ skills")
    assert has_term("c#", "C# and .NET")
    assert not has_term("intern", "internal tools")
    assert has_term("back-end", "back-end services")


def test_scores_and_title_double_counts() -> None:
    # python 8 + postgresql 4 from description; "backend"-style title has no keyword weights.
    assert score_job(make(), crit(), NOW) == 12
    assert score_job(make(title="Python Backend Engineer"), crit(), NOW) == 12 + 8


def test_below_min_score_rejected() -> None:
    assert score_job(make(description="We use Go."), crit(), NOW) is None


def test_title_filters() -> None:
    assert score_job(make(title="Staff Software Engineer"), crit(), NOW) is None
    assert score_job(make(title="Software Engineering Intern"), crit(), NOW) is None
    assert score_job(make(title="Account Executive"), crit(), NOW) is None


def test_location_and_remote() -> None:
    assert score_job(make(location="Austin, TX"), crit(), NOW) is None
    assert score_job(make(location="Austin, TX", remote=True), crit(), NOW) == 12
    no_remote = crit(accept_remote=False)
    assert score_job(make(location="Austin, TX", remote=True), no_remote, NOW) is None
    assert score_job(make(location="Remote - Germany", remote=True),
                     crit(exclude_locations=["germany"]), NOW) is None


def test_exclude_text_and_age() -> None:
    clearance = make(description="Python. Requires active TS/SCI clearance.")
    assert score_job(clearance, crit(exclude_text=["ts/sci"]), NOW) is None
    old = make(posted_at=NOW - timedelta(days=90))
    assert score_job(old, crit(max_age_days=45), NOW) is None
    assert score_job(old, crit(max_age_days=None), NOW) == 12
    assert score_job(make(posted_at=None), crit(max_age_days=45), NOW) == 12


def test_negative_keywords_clamp_at_zero() -> None:
    c = crit(keywords={"php": -20, "python": 8}, min_score=0)
    assert score_job(make(description="PHP and Python"), c, NOW) == 0
