"""Shared mock data and HTTP handlers (no network)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx

GREENHOUSE = {"jobs": [
    {"id": 1, "title": "Backend Software Engineer", "absolute_url": "https://gh/1",
     "location": {"name": "San Diego, CA"}, "first_published": "2026-11-01T10:00:00-08:00",
     "content": "&lt;p&gt;We use &lt;b&gt;Python&lt;/b&gt; &amp;amp; PostgreSQL.&lt;/p&gt;"},
    {"id": 2, "title": "Account Executive", "absolute_url": "https://gh/2",
     "location": {"name": "San Diego, CA"}, "content": "&lt;p&gt;Python&lt;/p&gt;"},
]}
LEVER = [
    {"id": "abc", "text": "Systems Software Engineer", "hostedUrl": "https://lever/abc",
     "categories": {"location": "Remote - US"}, "workplaceType": "remote",
     "createdAt": 1793700000000, "descriptionPlain": "Rust and Python, Postgres."},
]
ASHBY = {"jobs": [
    {"id": "x1", "title": "Platform Engineer", "jobUrl": "https://ashby/x1", "isRemote": True,
     "location": "Remote", "publishedAt": "2026-11-05T00:00:00.000+00:00",
     "descriptionHtml": "<p>Python, Docker, Linux.</p>"},
    {"id": "x2", "title": "Hidden Role", "isListed": False, "location": "Remote"},
]}
HN_SEARCH = {"hits": [
    {"objectID": "999", "title": "Ask HN: Who wants to be hired? (November 2026)"},
    {"objectID": "123", "title": "Ask HN: Who is hiring? (November 2026)",
     "created_at": "2026-11-02T16:00:00Z"},
]}
HN_THREAD = {"id": 123, "children": [
    {"id": 501, "author": "a", "text": "Acme (YC S22) | Backend Software Engineer | San Diego | "
     "ONSITE<p>We use Python and Postgres.</p>"},
    {"id": 502, "author": None, "text": "[dead]"},
    {"id": 503, "author": "b", "text": "Foo | Remote | Marketing Manager<p>Python</p>"},
]}


def ats_handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if host == "boards-api.greenhouse.io" and "/boards/goodco/" in path:
        return httpx.Response(200, json=GREENHOUSE)
    if host == "api.lever.co" and path.endswith("/leverco"):
        return httpx.Response(200, json=LEVER)
    if host == "api.ashbyhq.com" and path.endswith("/ashbyco"):
        return httpx.Response(200, json=ASHBY)
    if host == "hn.algolia.com" and path.endswith("/search_by_date"):
        return httpx.Response(200, json=HN_SEARCH)
    if host == "hn.algolia.com" and path.endswith("/items/123"):
        return httpx.Response(200, json=HN_THREAD)
    return httpx.Response(404, json={"error": "not found"})


class FakeOllama:
    """Mock Ollama + ATS routes. `fits` maps a title substring to the fit it should return."""

    def __init__(self, fits: dict[str, int], mode: str = "ok", models: list[str] | None = None):
        self.fits = fits
        self.mode = mode  # ok | down | badjson | think
        self.models = models if models is not None else ["qwen3:14b"]
        self.prompts: list[str] = []
        self.payloads: list[dict[str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "localhost":
            return ats_handler(request)
        if self.mode == "down":
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in self.models]})
        body = json.loads(request.content)
        prompt = body["messages"][-1]["content"]
        self.prompts.append(prompt)
        self.payloads.append(body)
        if self.mode == "badjson":
            content = "sorry, I cannot do that"
        else:
            fit = next((f for t, f in self.fits.items() if f"Title: {t}" in prompt), 50)
            content = json.dumps(
                {"fit": fit, "summary": f"fit {fit}", "red_flags": ["flag"] if fit < 60 else []}
            )
            if self.mode == "think":
                content = f"<think>reasoning...</think>\n{content}"
        return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})


def config_text(llm: bool, extra_llm: str = "") -> str:
    text = """
db: test.sqlite3
output: discovered.md
request_delay_seconds: 0
boards:
  greenhouse: [goodco, nosuchco]
  lever: [leverco]
  ashby: [ashbyco]
hn: true
criteria:
  include_title: [software, backend, platform, systems]
  exclude_title: [manager, sales]
  locations: [san diego, remote]
  min_score: 10
  max_age_days: 60
  keywords: {python: 8, postgres: 4, postgresql: 4, rust: 6, docker: 2, linux: 2}
"""
    if llm:
        text += f"""
llm:
  enabled: true
  model: qwen3:14b
  resume: resume.md
  min_fit: 50
  preferences: backend and systems roles
{extra_llm}"""
    return text


def write_config(tmp_path: Path, llm: bool, extra_llm: str = "") -> Path:
    (tmp_path / "resume.md").write_text("Yuri. Python, Java, Rust, FastAPI, PostgreSQL.")
    path = tmp_path / "config.yaml"
    path.write_text(config_text(llm, extra_llm))
    return path


Handler = Callable[[httpx.Request], httpx.Response]
