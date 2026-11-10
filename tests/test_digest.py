from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser

from jobpoller.digest import render_digest
from jobpoller.store import QueuedJob

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def qj(n: int, **kw: object) -> QueuedJob:
    base: dict[str, object] = dict(
        id=n, key=f"k{n}", status="new", status_updated="t", first_seen="t", company=f"Co{n}",
        title=f"Role {n}", url=f"https://jobs.example/{n}", score=10, source="ashby",
        location="San Diego, CA", fit=50, summary="", red_flags=(),
    )
    base.update(kw)
    return QueuedJob(**base)  # type: ignore[arg-type]


class Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def parse(doc: str) -> Tags:
    parser = Tags()
    parser.feed(doc)
    return parser


def test_sorted_by_fit_and_shows_core_fields() -> None:
    doc = render_digest(
        [qj(1, fit=60), qj(2, fit=92, summary="Great stack match", red_flags=("5+ years",)),
         qj(3, fit=None, score=33)],
        NOW,
    )
    order = [doc.index(f"Role {n}") for n in (2, 1, 3)]
    assert order == sorted(order)  # 92, 60, then keyword-only 33
    for expected in ("Great stack match", "5+ years", "San Diego, CA", "Co2", "ashby", "#2",
                     "Copy queue row", "https://jobs.example/2"):
        assert expected in doc
    assert 'data-fit="33"' in doc and "kw" in doc


def test_untrusted_text_is_escaped_and_cannot_inject() -> None:
    evil = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'
    doc = render_digest(
        [qj(1, title=evil, company=evil, location=evil, summary=evil, red_flags=(evil,),
            url="javascript:alert(3)"),
         qj(2, url='https://ok.example/"onmouseover="alert(4)')],
        NOW,
    )
    parsed = parse(doc)
    scripts = [t for t, _ in parsed.tags if t == "script"]
    assert len(scripts) == 1  # only our own fixed script
    assert "<script>alert" not in doc and "<img" not in doc
    for tag, attrs in parsed.tags:
        assert not any(name.startswith("on") for name in attrs), (tag, attrs)
        href = attrs.get("href")
        assert href is None or href.startswith("https://ok.example/")  # javascript: dropped


def test_is_self_contained() -> None:
    doc = render_digest([qj(1)], NOW)
    parsed = parse(doc)
    assert not [t for t, a in parsed.tags if t == "link" or "src" in a]
    assert "@import" not in doc and "url(" not in doc
    assert "Content-Security-Policy" in doc and "default-src 'none'" in doc
    assert 'name="viewport"' in doc and "prefers-color-scheme:dark" in doc
    for control in ('id="q"', 'id="min"', 'id="hide"'):
        assert control in doc


def test_copy_button_carries_exact_queue_row() -> None:
    doc = render_digest([qj(1, title="Eng | Backend", fit=88)], NOW)
    button = next(a for t, a in parse(doc).tags if t == "button")
    assert button["data-row"] == (
        "|  | Eng / Backend | Co1 | https://jobs.example/1 |  |  | discovered (fit 88) |"
    )


def test_dismissed_marked_and_empty_state() -> None:
    doc = render_digest([qj(1, status="dismissed")], NOW)
    assert 'data-status="dismissed"' in doc
    assert "Nothing queued." in render_digest([], NOW)
