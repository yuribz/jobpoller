"""Re-score cached jobs under edited criteria and show what would change. Pure: no I/O.

Keyword/filter side only (no LLM), so it is instant and works offline.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from .models import Job
from .scoring import Criteria, Verdict, explain_job


@dataclass(frozen=True)
class Row:
    job: Job
    before: Verdict | None  # None when there is no baseline to compare with
    after: Verdict


@dataclass
class TuneResult:
    total: int
    after_pass: int
    before_pass: int | None  # None without a baseline
    newly_pass: list[Row] = field(default_factory=list)
    newly_fail: list[Row] = field(default_factory=list)
    top: list[Row] = field(default_factory=list)  # best under the new criteria
    rejections: Counter[str] = field(default_factory=Counter)  # why jobs fail today


def rule_key(reason: str) -> str:
    """Group reasons so the breakdown stays short (drop per-job detail like the location)."""
    if reason.startswith("location:no match"):
        return "location:no match"
    if reason.startswith("age:"):
        return "age:too old"
    if reason.startswith("score "):
        return "score below min_score"
    return reason


def tune(
    jobs: Sequence[Job], after: Criteria, before: Criteria | None, now: datetime
) -> TuneResult:
    rows = [
        Row(
            job,
            explain_job(job, before, now) if before is not None else None,
            explain_job(job, after, now),
        )
        for job in jobs
    ]
    passing = [r for r in rows if r.after.passed]
    result = TuneResult(
        total=len(rows),
        after_pass=len(passing),
        before_pass=(
            sum(1 for r in rows if r.before is not None and r.before.passed)
            if before is not None
            else None
        ),
    )

    def score(v: Verdict | None) -> int:
        return v.score if v is not None and v.score is not None else 0

    result.newly_pass = sorted(
        (r for r in passing if r.before is not None and not r.before.passed),
        key=lambda r: (-score(r.after), r.job.key),
    )
    result.newly_fail = sorted(
        (r for r in rows if not r.after.passed and r.before is not None and r.before.passed),
        key=lambda r: (-score(r.before), r.job.key),
    )
    result.top = sorted(passing, key=lambda r: (-score(r.after), r.job.key))
    result.rejections = Counter(rule_key(r.after.reason) for r in rows if not r.after.passed)
    return result


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _line(row: Row, marker: str, note: str, inbox: set[str]) -> str:
    verdict = row.after if row.after.passed or row.before is None else row.before
    shown = verdict.score if verdict.score is not None else 0
    tail = "  [in your inbox]" if row.job.key in inbox else ""
    title = _clip(row.job.title, 52)
    company = _clip(row.job.company, 28)  # HN "company" is often a whole headline
    return f"  {marker} {shown:3d}  {title} @ {company} ({row.job.source}){note}{tail}"


def format_result(
    result: TuneResult, inbox: set[str], limit: int = 15, baseline_label: str | None = None
) -> str:
    out: list[str] = []
    if baseline_label is None:
        out.append(
            f"Passing: {result.after_pass} of {result.total} cached jobs.\n"
            "No baseline to compare with: run `jobpoller run` once (it records the criteria it\n"
            "used), or pass --against OLD_CONFIG.yaml."
        )
    else:
        out.append(f"Baseline: {baseline_label}")
        gained, lost = len(result.newly_pass), len(result.newly_fail)
        out.append(
            f"Passing: {result.before_pass} -> {result.after_pass} of {result.total} cached jobs "
            f"(+{gained} newly pass, -{lost} newly fail)"
        )

    def section(title: str, rows: list[Row], marker: str, note: str) -> None:
        if not rows:
            return
        suffix = f" (showing {limit} of {len(rows)})" if len(rows) > limit else ""
        out.append(f"\n{title}{suffix}:")
        for row in rows[:limit]:
            if note == "was":
                detail = f"   was: {row.before.reason}" if row.before else ""
            else:
                detail = f"   now: {row.after.reason}"
            out.append(_line(row, marker, detail, inbox))

    section("Newly passing", result.newly_pass, "+", "was")
    section("Newly failing", result.newly_fail, "-", "now")

    if result.rejections:
        out.append("\nWhere jobs are rejected today:")
        for reason, count in result.rejections.most_common(8):
            out.append(f"  {count:5d}  {reason}")

    if result.top:
        out.append(f"\nTop {min(limit, len(result.top))} under current settings:")
        for row in result.top[:limit]:
            out.append(_line(row, " ", "", inbox))
    else:
        out.append("\nNothing passes under current settings.")
    return "\n".join(out)
