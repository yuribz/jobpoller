"""`jobpoller add`: postings you saved yourself (txt, md, html), incl. LinkedIn-style pages."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from jobpoller.config import load_config
from jobpoller.manual import (
    ManualError,
    build_job,
    decode,
    extract,
    kind_for,
    normalize_url,
    read_file,
)
from jobpoller.store import Store
from tests.fixtures import FakeOllama
from tests.test_workflow import FITS, QUEUE, cli, setup

BODY = "We need a backend engineer to build Python services. " * 8

_DESC = (
    "&lt;p&gt;Build &lt;b&gt;Python&lt;/b&gt; services.&lt;/p&gt;"
    "&lt;ul&gt;&lt;li&gt;APIs&lt;/li&gt;&lt;/ul&gt;"
)
_LD = json.dumps(
    {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Backend Engineer &amp; Platform",
        "hiringOrganization": {"@type": "Organization", "name": "Acme Robotics"},
        "jobLocation": {
            "address": {
                "addressLocality": "San Diego",
                "addressRegion": "CA",
                "addressCountry": {"name": "US"},
            }
        },
        "datePosted": "2026-11-01T00:00:00Z",
        "description": _DESC,
    }
)
LINKEDIN_HTML = (
    "<!doctype html><html><head><title>Acme hiring Backend Engineer | LinkedIn</title>"
    "<style>.x{color:red}</style>"
    f'<script type="application/ld+json">{_LD}</script>'
    "<script>alert('x')</script></head>"
    "<body><nav>Sign in</nav><main><h1>Backend Engineer</h1><p>Some page text</p></main>"
    "</body></html>"
)

PLAIN_HTML = """<html><head><title>t</title><style>p{}</style></head><body>
<script>var secret = 1;</script><h1>Role</h1><p>Build &amp; ship</p><ul><li>Python</li>
<li>SQL</li></ul><!-- comment --></body></html>"""


def test_html_json_ld_gives_text_and_hints() -> None:
    ex = extract(LINKEDIN_HTML, "html")
    assert ex.title == "Backend Engineer & Platform" and ex.company == "Acme Robotics"
    assert ex.location == "San Diego, CA, US"
    assert ex.posted_at is not None and ex.posted_at.year == 2026
    assert "Build Python services." in ex.text and "APIs" in ex.text
    assert "<" not in ex.text and "Sign in" not in ex.text  # JSON-LD text, not page chrome


def test_plain_html_drops_scripts_styles_and_keeps_structure() -> None:
    text = extract(PLAIN_HTML, "html").text
    assert "secret" not in text and "p{}" not in text and "comment" not in text
    assert "Build & ship" in text
    assert text.splitlines() == ["Role", "Build & ship", "Python", "SQL"]


def test_text_and_md_pass_through_and_decode_falls_back() -> None:
    assert extract("# Role\r\n\r\n- Python\r\n", "text").text == "# Role\n\n- Python"
    assert decode("café".encode("cp1252")) == "café"
    assert decode(b"\xef\xbb\xbfhello") == "hello"  # BOM dropped


def test_file_kinds_and_clear_errors(tmp_path: Path) -> None:
    assert [kind_for(Path(n)) for n in ("a.TXT", "a.md", "a.html", "a.htm")] == [
        "text", "text", "html", "html"
    ]  # fmt: skip
    with pytest.raises(ManualError, match="pdftotext"):
        kind_for(Path("job.pdf"))
    with pytest.raises(ManualError, match="cannot read"):
        read_file(tmp_path / "missing.txt")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.linkedin.com/jobs/view/3812345678/?trk=public&refId=x", None),
        ("https://www.linkedin.com/jobs/view/backend-engineer-at-acme-3812345678?trk=a", None),
        ("https://www.linkedin.com/jobs/collections/recommended/?currentJobId=3812345678", None),
        ("https://boards.greenhouse.io/Acme/jobs/55/?utm_source=x&gh_jid=55#apply",
         "https://boards.greenhouse.io/Acme/jobs/55?gh_jid=55"),
    ],
)  # fmt: skip
def test_normalize_url(raw: str, expected: str | None) -> None:
    assert normalize_url(raw) == (expected or "https://www.linkedin.com/jobs/view/3812345678/")


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "linkedin.com/jobs/1", "ftp://x/y", ""])
def test_normalize_url_rejects_non_http(bad: str) -> None:
    with pytest.raises(ManualError):
        normalize_url(bad)


def test_build_job_flags_override_hints_and_missing_fields_error() -> None:
    ex = extract(LINKEDIN_HTML, "html")
    job, warns = build_job(ex, company="Override", title="", location="", url="https://a.io/j/1")
    assert (job.company, job.title, job.source) == (
        "Override",
        "Backend Engineer & Platform",
        "manual",
    )
    assert len(warns) == 1 and job.url == "https://a.io/j/1"  # the fixture text is short
    with pytest.raises(ManualError, match="--url"):
        build_job(extract(BODY, "text"), company="A", title="T", location="", url="")
    with pytest.raises(ManualError, match="empty"):
        build_job(extract("  ", "text"), company="A", title="T", location="", url="https://a.io")
    _, short = build_job(extract("tiny", "text"), company="A", title="T", location="",
                         url="https://a.io")  # fmt: skip
    assert "only 4 characters" in short[0]


# --- CLI ------------------------------------------------------------------------------------


def add_args(url: str = "https://www.linkedin.com/jobs/view/42/?trk=x") -> list[str]:
    return ["add", "--company", "Acme", "--title", "Backend Software Engineer", "--url", url]


def test_add_file_then_show_json_is_resume_tailor_compatible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = setup(tmp_path)
    posting = tmp_path / "job.md"
    posting.write_text(BODY)
    assert cli(path, *add_args(), str(posting), "--location", "Remote") == 0
    assert "added #1" in capsys.readouterr().out
    assert cli(path, "show", "1", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["source"] == "manual" and data["description"] == BODY.strip()
    assert data["url"] == "https://www.linkedin.com/jobs/view/42/" and data["id"] == 1
    assert data["location"] == "Remote" and data["status"] == "new" and data["fit"] is None
    assert data["keyword_score"] > 0  # python keyword scored, filters not applied as a veto
    assert cli(path, "list") == 0
    assert "(manual)" in capsys.readouterr().out


def test_add_is_idempotent_by_url_even_across_sources(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = setup(tmp_path)
    posting = tmp_path / "job.txt"
    posting.write_text(BODY)
    assert cli(path, *add_args(), str(posting)) == 0
    assert cli(path, *add_args("https://www.linkedin.com/jobs/view/42/?utm_source=z"),
               str(posting)) == 0  # fmt: skip
    assert "already queued as #1" in capsys.readouterr().out
    store = Store(load_config(path).db)
    assert len(store.list_jobs()) == 1
    # A job the poller found itself is recognised by its apply URL too.
    assert cli(path, "run") == 0
    total = len(store.list_jobs())
    found = next(j for j in store.list_jobs() if j.source != "manual")
    again = tmp_path / "again.txt"
    again.write_text(BODY)
    assert cli(path, "add", "--company", "X", "--title", "Y", "--url", found.url, str(again)) == 0
    assert len(store.list_jobs()) == total  # no duplicate row
    assert "already queued" in capsys.readouterr().out


def test_add_html_from_stdin_and_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import sys

    path = setup(tmp_path)

    class Stdin(io.TextIOWrapper):
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(sys, "stdin", Stdin(io.BytesIO(LINKEDIN_HTML.encode())))
    code = cli(
        path, "add", "--format", "html", "--url", "https://www.linkedin.com/jobs/view/7/", "-"
    )
    assert code == 0
    store = Store(load_config(path).db)
    row = store.list_jobs()[0]
    assert (row.company, row.title) == ("Acme Robotics", "Backend Engineer & Platform")
    capsys.readouterr()
    assert cli(path, "add", str(tmp_path / "nope.txt"), "--company", "A") == 1
    assert "cannot read" in capsys.readouterr().err
    posting = tmp_path / "p.txt"
    posting.write_text(BODY)
    assert cli(path, "add", str(posting), "--company", "A", "--title", "T") == 1
    assert "--url" in capsys.readouterr().err


def test_add_scores_with_llm_and_survives_llm_down(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = setup(tmp_path, llm=True)
    posting = tmp_path / "job.txt"
    posting.write_text(BODY)
    fake = FakeOllama(FITS)
    assert cli(path, *add_args(), str(posting), transport=httpx.MockTransport(fake)) == 0
    assert "fit 85" in capsys.readouterr().out
    down = FakeOllama(FITS, mode="down")
    assert (
        cli(path, *add_args("https://a.io/2"), str(posting), transport=httpx.MockTransport(down))
        == 0
    )
    captured = capsys.readouterr()
    assert "not scored" in captured.err and "added #2" in captured.out
    store = Store(load_config(path).db)
    assert [j.fit for j in store.list_jobs()] == [85, None]


def test_add_no_score_skips_llm_and_promote_writes_queue(tmp_path: Path) -> None:
    (tmp_path / "queue.md").write_text(QUEUE)
    path = setup(tmp_path, "queue_path: queue.md\n", llm=True)
    posting = tmp_path / "job.txt"
    posting.write_text(BODY)
    fake = FakeOllama(FITS)
    code = cli(path, *add_args(), str(posting), "--no-score", "--promote",
               transport=httpx.MockTransport(fake))  # fmt: skip
    assert code == 0 and fake.prompts == []
    assert "https://www.linkedin.com/jobs/view/42/" in (tmp_path / "queue.md").read_text()
    assert Store(load_config(path).db).list_jobs()[0].status == "promoted"
