"""Render the job list as Markdown (pure; no I/O). Used by `jobpoller list -m`."""

from __future__ import annotations

import re
from collections.abc import Sequence

from .models import STATUSES
from .store import QueuedJob

_SPECIAL = re.compile(r"([\\`*_{}\[\]<>#|~])")


def _esc(text: str, limit: int = 0) -> str:
    """Escape Markdown syntax and flatten whitespace so scraped text can't break the layout."""
    flat = " ".join(text.split())
    if limit and len(flat) > limit:
        flat = flat[: limit - 1].rstrip() + "…"
    return _SPECIAL.sub(r"\\\1", flat)


def _link(url: str) -> str | None:
    # Only http(s); angle brackets keep URLs with parentheses intact.
    if url.startswith(("http://", "https://")) and not any(c in url for c in " <>\n"):
        return f"[Apply]({'<' + url + '>'})"
    return None


def render_list_md(jobs: Sequence[QueuedJob]) -> str:
    """Group by status (STATUSES order), jobs already sorted best-first by the store."""
    noun = "job" if len(jobs) == 1 else "jobs"
    out = [f"# jobpoller: {len(jobs)} {noun}", ""]
    for status in STATUSES:
        group = [j for j in jobs if j.status == status]
        if not group:
            continue
        out += [f"## {status} ({len(group)})", ""]
        for j in group:
            out += [f"### `#{j.id}` {_esc(j.title, 90)} @ **{_esc(j.company)}**", ""]
            score = f"**fit {j.fit}**" if j.fit is not None else f"keyword {j.score}"
            meta = [score, f"`{j.source}`"]
            if j.location:
                meta.append(_esc(j.location, 40))
            link = _link(j.url)
            if link:
                meta.append(link)
            out += [" · ".join(meta), ""]
            if j.summary:
                out += [f"> {_esc(j.summary)}", ""]
            if j.red_flags:
                out += ["**Red flags:** " + "; ".join(f"`{_esc_code(f)}`" for f in j.red_flags), ""]
        out += ["---", ""]
    if out[-2:] == ["---", ""]:
        out = out[:-2]
    return "\n".join(out).rstrip() + "\n"


def _esc_code(text: str) -> str:
    return " ".join(text.replace("`", "'").split())
