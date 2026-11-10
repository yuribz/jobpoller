from __future__ import annotations

import os
from pathlib import Path

import pytest

from jobpoller.queuefile import (
    HEADER,
    QueueError,
    add_to_queue_file,
    append_row,
    format_row,
)

HAND = """# Job queue

Notes I wrote by hand.

| # | Role | Company | Apply link | Resume | Cover letter | Status |
|---|------|---------|------------|--------|--------------|--------|
| 1 | SWE | Acme  | https://acme/1 | acme.pdf | acme-cl.pdf | applied |
| 2 | Backend | Foo | https://foo/2 |  |  | interview 11/12 |
| 7 | Odd numbering | Bar | https://bar/7 | x | y | rejected |

## Later section

keep me
"""


def add(text: str, url: str = "https://new/1") -> tuple[str, int | None]:
    return append_row(text, "Platform Engineer", "Newco", url, "discovered (fit 88)")


def test_row_format_is_exact() -> None:
    row = format_row(4, "Role", "Co", "https://x", "discovered (fit 85)")
    assert row == "| 4 | Role | Co | https://x |  |  | discovered (fit 85) |"


def test_appends_after_last_row_continuing_numbering_without_touching_others() -> None:
    new, number = add(HAND)
    assert number == 8  # continues from the highest number, not the row count
    old_lines, new_lines = HAND.split("\n"), new.split("\n")
    idx = old_lines.index("| 7 | Odd numbering | Bar | https://bar/7 | x | y | rejected |") + 1
    assert new_lines[idx] == (
        "| 8 | Platform Engineer | Newco | https://new/1 |  |  | discovered (fit 88) |"
    )
    assert new_lines[:idx] + new_lines[idx + 1 :] == old_lines  # everything else byte-identical


def test_creates_section_when_no_table() -> None:
    new, number = add("# My queue\n\nsome notes\n")
    assert number == 1
    assert new.startswith("# My queue\n\nsome notes\n\n## Discovered by jobpoller\n\n" + HEADER)
    assert new.endswith(
        "| 1 | Platform Engineer | Newco | https://new/1 |  |  | discovered (fit 88) |\n"
    )
    again, number2 = add(new, url="https://new/2")
    assert number2 == 2 and again.count("## Discovered by jobpoller") == 1  # reuses the table


def test_empty_text_and_missing_trailing_newline() -> None:
    new, number = add("")
    assert number == 1 and new.startswith("## Discovered by jobpoller")
    table_only = HAND.split("\n## Later")[0].rstrip("\n")  # table is the last thing, no newline
    out, n = add(table_only)
    assert n == 8 and out.endswith("discovered (fit 88) |\n")


def test_preserves_crlf() -> None:
    crlf = HAND.replace("\n", "\r\n")
    new, _ = add(crlf)
    assert new.replace("\r\n", "").count("\n") == 0  # no bare LF introduced
    assert new.count("\r\n") == crlf.count("\r\n") + 1


def test_duplicate_url_is_a_noop() -> None:
    once, _ = add(HAND)
    twice, number = add(once)
    assert number is None and twice == once
    hand_added, number = add(HAND, url="https://foo/2")  # already there from a hand-written row
    assert number is None and hand_added == HAND


def test_cells_cannot_break_the_table() -> None:
    row = format_row(1, "Eng | Backend\nTeam", "A|B", "https://x/?a=1|2", "s")
    assert row.count("|") == 8 and "\n" not in row


def test_add_to_queue_file_creates_and_updates_atomically(tmp_path: Path) -> None:
    path = tmp_path / "queue.md"
    assert add_to_queue_file(path, "R", "C", "https://u/1", "discovered (fit 80)") == 1
    os.chmod(path, 0o600)
    assert add_to_queue_file(path, "R2", "C2", "https://u/2", "discovered (fit 70)") == 2
    assert add_to_queue_file(path, "R2", "C2", "https://u/2", "discovered (fit 70)") is None
    assert path.stat().st_mode & 0o777 == 0o600  # mode survives the rename
    assert [p.name for p in tmp_path.iterdir()] == ["queue.md"]  # no temp files left
    with pytest.raises(QueueError):
        add_to_queue_file(tmp_path / "nope" / "q.md", "R", "C", "https://u/3", "s")


def test_long_free_form_cells_are_capped() -> None:
    row = format_row(1, "x" * 300, "y" * 300, "https://u", "s")
    role, company = row.split("|")[2].strip(), row.split("|")[3].strip()
    assert len(role) == 80 and role.endswith("…") and len(company) == 40
