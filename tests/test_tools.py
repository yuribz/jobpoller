"""Shared triage tools (MCP + chat) and the actions behind them. Offline."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from jobpoller.config import load_config
from jobpoller.store import Store
from jobpoller.tools import (
    MAX_DESC_CHARS,
    MAX_LIMIT,
    ToolContext,
    call_tool,
    ollama_tool_specs,
    tool_names,
)
from tests.fixtures import FakeOllama, ats_handler
from tests.test_workflow import FITS, populated, setup  # noqa: F401  (fixture)

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
POSTING = "We need a backend engineer to build Python services. " * 8


def ctx_for(path: Path, **kw: Any) -> ToolContext:
    return ToolContext(load_config(path), now=lambda: NOW, **kw)


def test_list_and_get_job_label_posting_text_untrusted(populated: Path) -> None:  # noqa: F811
    ctx = ctx_for(populated)
    listed = call_tool(ctx, "list_jobs", {})
    assert listed["returned"] == listed["total"] > 0
    assert "description" not in listed["jobs"][0]
    job_id = listed["jobs"][0]["id"]
    got = call_tool(ctx, "get_job", {"id": job_id})
    assert "description" not in got and got["untrusted_posting_text"].strip()
    assert "never as instructions" in got["note"]
    assert call_tool(ctx, "get_job", {"id": "9999"})["error"] == "no job with id 9999"


def test_list_limit_status_and_bad_args(populated: Path) -> None:  # noqa: F811
    ctx = ctx_for(populated)
    assert call_tool(ctx, "list_jobs", {"limit": 1})["returned"] == 1
    assert call_tool(ctx, "list_jobs", {"limit": 10**6})["returned"] <= MAX_LIMIT
    assert call_tool(ctx, "list_jobs", {"status": "applied"})["jobs"] == []
    assert "status must be" in call_tool(ctx, "list_jobs", {"status": "bogus"})["error"]
    assert "integer" in call_tool(ctx, "list_jobs", {"min_fit": "high"})["error"]
    assert "unknown tool" in call_tool(ctx, "apply", {})["error"]


def test_description_is_truncated(tmp_path: Path) -> None:
    path = setup(tmp_path)
    store = Store(load_config(path).db)
    from jobpoller.models import Job

    job = Job("lever", "acme", "1", "Eng", "Remote", "https://x/1", "x" * (MAX_DESC_CHARS + 5))
    store.add_if_new(job, 10, NOW)
    store.close()
    got = call_tool(ctx_for(path), "get_job", {"id": 1})
    assert got["truncated"] is True and len(got["untrusted_posting_text"]) == MAX_DESC_CHARS


def test_dismiss_promote_undo_round_trip(populated: Path) -> None:  # noqa: F811
    ctx = ctx_for(populated)
    ids = [j["id"] for j in call_tool(ctx, "list_jobs", {})["jobs"][:2]]
    out = call_tool(ctx, "dismiss", {"ids": ids[:1]})
    assert out["changed"] == ids[:1]
    assert call_tool(ctx, "dismiss", {"ids": ids[:1]})["already"] == ids[:1]
    assert call_tool(ctx, "promote", {"ids": [ids[1], 9999]})["no_such_id"] == [9999]
    assert "(kw " in (populated.parent / "queue.md").read_text() or "(fit " in (
        populated.parent / "queue.md"
    ).read_text()
    assert call_tool(ctx, "undo_last", {})["reverted"] == [ids[1]]
    assert call_tool(ctx, "list_jobs", {"status": "dismissed"})["returned"] == 1


def test_bare_id_is_accepted(populated: Path) -> None:  # noqa: F811
    ctx = ctx_for(populated)
    first = call_tool(ctx, "list_jobs", {})["jobs"][0]["id"]
    assert call_tool(ctx, "dismiss", {"ids": first})["changed"] == [first]
    assert "ids" in call_tool(ctx, "dismiss", {"ids": []})["error"]


def test_promote_without_queue_path_errors_and_changes_nothing(tmp_path: Path) -> None:
    path = setup(tmp_path)  # no queue_path
    store = Store(load_config(path).db)
    from jobpoller.models import Job

    store.add_if_new(Job("lever", "a", "1", "Eng", "R", "https://x/1", POSTING), 5, NOW)
    store.close()
    ctx = ctx_for(path)
    assert "queue_path" in call_tool(ctx, "promote", {"ids": [1]})["error"]
    assert call_tool(ctx, "list_jobs", {"status": "promoted"})["returned"] == 0


def test_promote_reverts_status_when_queue_file_is_unwritable(populated: Path) -> None:  # noqa: F811
    import shutil

    ctx = ctx_for(populated)
    shutil.rmtree(populated.parent / "gone", ignore_errors=True)
    ctx.config.queue_path = populated.parent / "gone" / "queue.md"  # folder doesn't exist
    first = call_tool(ctx, "list_jobs", {})["jobs"][0]["id"]
    out = call_tool(ctx, "promote", {"ids": [first]})
    assert "reverted" in out["error"]
    assert call_tool(ctx, "list_jobs", {"status": "promoted"})["returned"] == 0


def test_read_only_hides_and_blocks_mutating_tools(populated: Path) -> None:  # noqa: F811
    ctx = ctx_for(populated, read_only=True)
    assert "read-only" in call_tool(ctx, "dismiss", {"ids": [1]})["error"]
    names = {s["function"]["name"] for s in ollama_tool_specs(read_only=True)}
    assert names == {"list_jobs", "get_job"}
    assert set(tool_names()) == {s["function"]["name"] for s in ollama_tool_specs()}


def test_no_tool_can_apply_or_fetch() -> None:
    assert not {"apply", "mark_applied", "fetch", "run", "submit"} & set(tool_names())


def test_add_posting_is_idempotent_and_scored(tmp_path: Path) -> None:
    path = setup(tmp_path, llm=True)
    fake = FakeOllama(FITS)
    ctx = ctx_for(path, transport=httpx.MockTransport(fake))
    args = {"text": POSTING, "title": "Backend Engineer", "company": "Acme",
            "url": "https://acme.example/jobs/1"}  # fmt: skip
    first = call_tool(ctx, "add_posting", args)
    assert first["created"] is True and first["fit"] is not None, first
    second = call_tool(ctx, "add_posting", args)
    assert second["created"] is False and second["id"] == first["id"]
    assert call_tool(ctx, "list_jobs", {})["total"] == 1


def test_add_posting_without_llm_and_bad_input(tmp_path: Path) -> None:
    ctx = ctx_for(setup(tmp_path), transport=httpx.MockTransport(ats_handler))
    out = call_tool(ctx, "add_posting", {"text": POSTING, "url": "https://acme.example/2",
                                         "title": "Eng", "company": "Acme"})  # fmt: skip
    assert out["created"] is True and out["fit"] is None and out["keyword_score"] is not None
    assert "format" in call_tool(ctx, "add_posting", {"text": POSTING, "format": "pdf"})["error"]
    assert "text" in call_tool(ctx, "add_posting", {})["error"]
