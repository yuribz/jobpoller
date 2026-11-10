"""Turn a posting you saved or pasted yourself (txt, md, html) into a `Job`. Pure: no network.

Nothing here fetches a page. The text comes from a file or stdin you provide, so this stays
inside the "public endpoints only" rule even for sites jobpoller must never scrape (LinkedIn).
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Job
from .sources import _parse_iso, strip_html

TEXT_SUFFIXES = {".txt", ".text", ".md", ".markdown"}
HTML_SUFFIXES = {".html", ".htm"}
MAX_BYTES = 2_000_000
MAX_CHARS = 100_000
SHORT_POSTING_CHARS = 200

_SKIP = {"script", "style", "noscript", "svg", "template", "head", "iframe"}
_BLOCK = {
    "p", "div", "br", "li", "ul", "ol", "tr", "section", "article", "header", "footer",
    "h1", "h2", "h3", "h4", "h5", "h6", "table", "main", "dd", "dt",
}  # fmt: skip
_TRACKING = {
    "trk", "trackingid", "refid", "gh_src", "lever-source", "lever-origin", "gclid", "fbclid",
    "originalsubdomain", "ebp", "midtoken", "midsig", "trkemail", "otptoken", "currentjobid",
    "recommendedflavor", "lipi", "licu",
}  # fmt: skip


class ManualError(Exception):
    """The input can't become a job; the message says what to fix."""


@dataclass(frozen=True)
class Extracted:
    text: str
    title: str = ""
    company: str = ""
    location: str = ""
    url: str = ""
    posted_at: datetime | None = None


# --- html -------------------------------------------------------------------------------


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.ld: list[str] = []
        self._skip_depth = 0
        self._in_ld = False
        self._ld_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and dict(attrs).get("type", "") == "application/ld+json":
            self._in_ld, self._ld_buf = True, []
        if tag in _SKIP:
            self._skip_depth += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_ld:
            self.ld.append("".join(self._ld_buf))
            self._in_ld = False
        if tag in _SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_ld:
            self._ld_buf.append(data)
        elif not self._skip_depth:
            self.parts.append(data)


def _clean(text: str) -> str:
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"[ \t]*\n[ \t\n]*", "\n", text)
    return text.strip()


def _job_posting(blob: Any) -> dict[str, Any] | None:
    """Find a schema.org JobPosting anywhere in a JSON-LD value."""
    if isinstance(blob, list):
        for item in blob:
            found = _job_posting(item)
            if found:
                return found
    elif isinstance(blob, dict):
        kind = blob.get("@type")
        if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
            return blob
        return _job_posting(blob.get("@graph"))
    return None


def _name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "")
    return value if isinstance(value, str) else ""


def _ld_location(posting: dict[str, Any]) -> str:
    places = posting.get("jobLocation")
    places = places if isinstance(places, list) else [places]
    found: list[str] = []
    for place in places:
        addr = place.get("address") if isinstance(place, dict) else None
        if isinstance(addr, dict):
            keys = ("addressLocality", "addressRegion", "addressCountry")
            bits = [addr.get(k) for k in keys]
            found.append(", ".join(_name(b) or str(b) for b in bits if b))
    if str(posting.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        found.append("Remote")
    return "; ".join(f for f in found if f)


def _from_html(raw: str) -> Extracted:
    page = _Page()
    page.feed(raw)
    page.close()
    for blob in page.ld:
        try:
            posting = _job_posting(json.loads(blob))
        except ValueError:
            continue
        if posting is None:
            continue
        description = str(posting.get("description") or "")
        if "&lt;" in description:  # double-encoded HTML is common in JobPosting markup
            description = html.unescape(description)
        text = strip_html(description) if description else _clean("".join(page.parts))
        return Extracted(
            text=text,
            title=html.unescape(str(posting.get("title") or "")).strip(),
            company=html.unescape(_name(posting.get("hiringOrganization"))).strip(),
            location=_ld_location(posting),
            url=str(posting.get("url") or ""),
            posted_at=_parse_iso(posting.get("datePosted")),
        )
    return Extracted(text=_clean("".join(page.parts)))


# --- public API -------------------------------------------------------------------------


def decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", "replace")  # cp1252 leaves a few bytes undefined


def extract(raw: str, kind: str) -> Extracted:
    """`kind` is "text" (txt/md/pasted) or "html"."""
    if kind not in ("text", "html"):
        raise ValueError(f"kind must be text or html, got {kind!r}")
    ex = _from_html(raw) if kind == "html" else Extracted(text=raw.replace("\r\n", "\n").strip())
    if len(ex.text) > MAX_CHARS:
        ex = Extracted(ex.text[:MAX_CHARS], ex.title, ex.company, ex.location, ex.url, ex.posted_at)
    return ex


def kind_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in HTML_SUFFIXES:
        return "html"
    if suffix in TEXT_SUFFIXES:
        return "text"
    hint = (
        " (convert it to text first, e.g. `pdftotext file.pdf`)"
        if suffix in {".pdf", ".docx", ".doc", ".rtf"}
        else ""
    )
    raise ManualError(
        f"unsupported file type '{suffix or path.name}': use .txt, .md or .html{hint}"
    )


def read_file(path: Path, kind: str | None = None) -> Extracted:
    kind = kind or kind_for(path)
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ManualError(f"{path} is larger than {MAX_BYTES // 1_000_000} MB; not a posting?")
        return extract(decode(path.read_bytes()), kind)
    except OSError as exc:
        raise ManualError(f"cannot read {path}: {exc.strerror or exc}") from exc


def normalize_url(url: str) -> str:
    """Strip tracking noise so the same posting always has the same URL (dedupe key)."""
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ManualError(f"url must start with http:// or https://, got {url!r}")
    if len(url) > 2000:
        raise ManualError("url is unreasonably long")
    host = parts.netloc.lower()
    query = parse_qsl(parts.query, keep_blank_values=False)
    if host.endswith("linkedin.com"):
        current = next((v for k, v in query if k.lower() == "currentjobid"), "")
        match = re.search(r"/jobs/view/(?:[^/?#]*-)?(\d+)", parts.path)
        job_id = match.group(1) if match else (current if current.isdigit() else "")
        if job_id:
            return f"https://www.linkedin.com/jobs/view/{job_id}/"
    kept = [
        (k, v)
        for k, v in query
        if not k.lower().startswith(("utm_", "mc_")) and k.lower() not in _TRACKING
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, host, path, urlencode(kept), ""))


def build_job(
    ex: Extracted, *, company: str, title: str, location: str, url: str
) -> tuple[Job, list[str]]:
    """Merge flags over what the file told us. Returns (job, warnings)."""
    company = (company or ex.company).strip()
    title = (title or ex.title).strip()
    url = url or ex.url
    missing = [n for n, v in (("--company", company), ("--title", title), ("--url", url)) if not v]
    if missing:
        raise ManualError(f"missing {', '.join(missing)} (the file did not contain it either)")
    if not ex.text.strip():
        raise ManualError("the posting text is empty")
    clean_url = normalize_url(url)
    warnings: list[str] = []
    if len(ex.text) < SHORT_POSTING_CHARS:
        warnings.append(
            f"posting text is only {len(ex.text)} characters; did you copy the whole description?"
        )
    job_id = hashlib.sha256(clean_url.encode()).hexdigest()[:12]
    job = Job(
        source="manual",
        company=company,
        job_id=job_id,
        title=title,
        location=(location or ex.location).strip(),
        url=clean_url,
        description=ex.text,
        posted_at=ex.posted_at,
    )
    return job, warnings
