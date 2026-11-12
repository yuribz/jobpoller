"""`jobpoller list -m`: Markdown output."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobpoller.listmd import render_list_md
from jobpoller.store import QueuedJob
from tests.test_workflow import cli, populated  # noqa: F401  (fixture)


def _job(**kw: object) -> QueuedJob:
    base: dict[str, object] = dict(
        id=3, key="k", status="new", status_updated="", first_seen="", company="Acme",
        title="Backend Engineer", url="https://x.test/a", score=10, source="lever",
        location="Remote", fit=85, summary="Good match.", red_flags=("on-call",),
    )  # fmt: skip
    base.update(kw)
    return QueuedJob(**base)  # type: ignore[arg-type]


def test_render_has_headers_flags_and_link() -> None:
    md = render_list_md([_job()])
    assert md.startswith("# jobpoller: 1 job\n")
    assert "## new (1)" in md
    assert "### `#3` Backend Engineer @ **Acme**" in md
    assert "**fit 85** · `lever` · Remote · [Apply](<https://x.test/a>)" in md
    assert "> Good match." in md
    assert "**Red flags:** `on-call`" in md


def test_render_escapes_markdown_and_rejects_bad_urls() -> None:
    md = render_list_md(
        [_job(title="*Bold* [x](y)\n# H", url="javascript:alert(1)", red_flags=("a`b",), fit=None)]
    )
    assert "\\*Bold\\* \\[x\\]" in md and "\n# H" not in md
    assert "javascript" not in md and "[Apply]" not in md
    assert "keyword 10" in md and "`a'b`" in md


def test_groups_follow_status_order() -> None:
    md = render_list_md([_job(id=1, status="applied"), _job(id=2, status="new")])
    assert md.index("## new") < md.index("## applied")


def test_list_m_flag(populated: Path, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    capsys.readouterr()
    assert cli(populated, "list", "-m") == 0
    out = capsys.readouterr().out
    assert out.startswith("# jobpoller: ") and "### `#" in out
