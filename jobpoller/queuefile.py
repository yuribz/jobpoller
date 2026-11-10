"""Append rows to the hand-maintained application queue (a markdown table).

The queue file belongs to the user. We only ever *insert one line* into the table (or add a
new section at the end); every other byte, including hand-written rows, is left as is.
Row format: `| # | Role | Company | Apply link | Resume | Cover letter | Status |`.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

SECTION = "## Discovered by jobpoller"
HEADER = "| # | Role | Company | Apply link | Resume | Cover letter | Status |"
SEPARATOR = "|---|---|---|---|---|---|---|"

_HEADER_RE = re.compile(
    r"^\s*\|\s*#\s*\|\s*Role\s*\|\s*Company\s*\|\s*Apply link\s*\|\s*Resume\s*\|"
    r"\s*Cover letter\s*\|\s*Status\s*\|\s*$",
    re.I,
)
_SEPARATOR_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


class QueueError(Exception):
    pass


def _cell(text: str, limit: int | None = None) -> str:
    cleaned = " ".join(text.replace("|", "/").split())
    return cleaned if limit is None or len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def format_row(number: int | None, role: str, company: str, url: str, status: str) -> str:
    """One table row; Resume and Cover letter stay blank for the user to fill in."""
    n = "" if number is None else str(number)
    # HN postings have free-form headlines; cap the cells so one cannot wreck the table.
    return (
        f"| {n} | {_cell(role, 80)} | {_cell(company, 40)} | {_cell(url)} |  |  | {_cell(status)} |"
    )


def append_row(text: str, role: str, company: str, url: str, status: str) -> tuple[str, int | None]:
    """Return (new_text, row_number); row_number is None if the URL is already in the file.

    Pure: the caller does the I/O. Numbering continues from the highest `#` in the table.
    """
    if url and url in text:
        return text, None
    eol = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(eol)  # lossless: eol.join(lines) == text
    if lines and lines[-1] == "":
        lines.pop()

    header_idx = next((i for i, line in enumerate(lines) if _HEADER_RE.match(line)), None)
    if header_idx is None:
        if lines and lines[-1].strip():
            lines.append("")
        number = 1
        lines += [SECTION, "", HEADER, SEPARATOR, format_row(number, role, company, url, status)]
    else:
        first = header_idx + 1
        if first < len(lines) and _SEPARATOR_RE.match(lines[first]):
            first += 1
        end = first
        while end < len(lines) and lines[end].lstrip().startswith("|"):
            end += 1
        numbers = [
            int(cells[1])
            for line in lines[first:end]
            if len(cells := [c.strip() for c in line.split("|")]) > 2 and cells[1].isdigit()
        ]
        number = max(numbers, default=0) + 1
        lines.insert(end, format_row(number, role, company, url, status))
    return eol.join(lines) + eol, number


def add_to_queue_file(
    path: Path, role: str, company: str, url: str, status: str
) -> int | None:
    """Insert a row into `path` atomically (temp file + rename); returns the row number."""
    if not path.parent.is_dir():
        raise QueueError(f"queue folder does not exist: {path.parent}")
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            text = fh.read()
    except FileNotFoundError:
        text = ""
    except OSError as exc:
        raise QueueError(f"cannot read {path}: {exc}") from exc
    new_text, number = append_row(text, role, company, url, status)
    if number is None:
        return None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(new_text)
        if path.exists():
            shutil.copymode(path, tmp_name)
        os.replace(tmp_name, path)
    except OSError as exc:
        raise QueueError(f"cannot write {path}: {exc}") from exc
    return number
