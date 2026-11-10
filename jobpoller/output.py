from __future__ import annotations

from datetime import date
from pathlib import Path

from .models import Match, Ranking

_HEADER = (
    "# Discovered jobs (jobpoller)\n\n"
    "Fit = local-LLM score vs your resume (blank if LLM ranking is off); KW = keyword score.\n"
    "Promote the good ones into queue/job_queue.md after tailoring.\n\n"
    "| Date | Fit | KW | Role | Company | Location | Apply link | Source | Notes |\n"
    "|---|---|---|---|---|---|---|---|---|\n"
)


def _cell(text: str, limit: int = 70) -> str:
    cleaned = " ".join(text.replace("|", "/").split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def _notes(ranking: Ranking | None) -> str:
    if ranking is None:
        return ""
    text = ranking.summary
    if ranking.red_flags:
        text += " ⚠ " + "; ".join(ranking.red_flags)
    return _cell(text, 200)


def append_discovered(path: Path, matches: list[Match], today: date) -> None:
    """Append rows (best first) to a markdown table, creating it if needed."""
    if not matches:
        return
    if not path.exists():
        path.write_text(_HEADER, encoding="utf-8")
    lines = []
    for m in sorted(matches, key=lambda x: -x.sort_key):
        fit = str(m.ranking.fit) if m.ranking else ""
        lines.append(
            f"| {today.isoformat()} | {fit} | {m.score} | {_cell(m.job.title)} "
            f"| {_cell(m.job.company, 40)} | {_cell(m.job.location, 40)} | {m.job.url} "
            f"| {m.job.source} | {_notes(m.ranking)} |\n"
        )
    with path.open("a", encoding="utf-8") as fh:
        fh.writelines(lines)
