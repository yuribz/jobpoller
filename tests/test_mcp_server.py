"""`jobpoller mcp`: in-memory client session plus a real stdio subprocess run."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

pytest.importorskip("mcp")

from mcp import Client, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from jobpoller.cli import main  # noqa: E402
from jobpoller.config import load_config  # noqa: E402
from jobpoller.mcp_server import build_server  # noqa: E402
from jobpoller.tools import ToolContext  # noqa: E402
from tests.fixtures import ats_handler  # noqa: E402
from tests.test_workflow import populated  # noqa: E402, F401  (fixture)

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
POSTING = "We need a backend engineer to build Python services. " * 8


def payload(result: Any) -> dict[str, Any]:
    if getattr(result, "structured_content", None):
        return dict(result.structured_content)
    return dict(json.loads(result.content[0].text))


def test_triage_session_in_memory(populated: Path) -> None:  # noqa: F811
    ctx = ToolContext(load_config(populated), now=lambda: NOW,
                      transport=httpx.MockTransport(ats_handler))  # fmt: skip

    async def go() -> None:
        async with Client(build_server(ctx)) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert names == {"list_jobs", "get_job", "dismiss", "promote", "undo_last",
                             "add_posting"}  # fmt: skip
            listed = payload(await client.call_tool("list_jobs", {"limit": 2}))
            ids = [j["id"] for j in listed["jobs"]]
            got = payload(await client.call_tool("get_job", {"id": ids[0]}))
            assert got["untrusted_posting_text"] and "description" not in got
            dismissed = payload(await client.call_tool("dismiss", {"ids": ids[:1]}))
            assert dismissed["changed"] == ids[:1]
            promoted = payload(await client.call_tool("promote", {"ids": ids[1:]}))
            assert promoted["changed"] == ids[1:]
            assert payload(await client.call_tool("undo_last", {}))["reverted"] == ids[1:]
            added = payload(await client.call_tool(
                "add_posting",
                {"text": POSTING, "title": "Eng", "company": "Acme", "url": "https://a.example/1"},
            ))
            assert added["created"] is True
            bad = await client.call_tool("get_job", {"id": 9999})
            assert bad.is_error
            resource = await client.read_resource(f"jobpoller://job/{ids[0]}")
            assert json.loads(resource.contents[0].text)["id"] == ids[0]
            prompts = {p.name for p in (await client.list_prompts()).prompts}
            assert prompts == {"triage"}

    anyio.run(go)


def test_read_only_server_has_no_write_tools(populated: Path) -> None:  # noqa: F811
    ctx = ToolContext(load_config(populated), read_only=True)

    async def go() -> None:
        async with Client(build_server(ctx)) as client:
            assert {t.name for t in (await client.list_tools()).tools} == {"list_jobs", "get_job"}

    anyio.run(go)


def test_stdio_subprocess_keeps_stdout_clean(populated: Path) -> None:  # noqa: F811
    """What Claude Desktop does: spawn `jobpoller mcp` and speak JSON-RPC over its stdio."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jobpoller", "mcp", "--config", str(populated), "--read-only"],
    )

    async def go() -> None:
        async with stdio_client(params) as streams:
            from mcp import ClientSession

            async with ClientSession(*streams) as session:
                await session.initialize()
                result = await session.call_tool("list_jobs", {"limit": 1})
                assert not result.is_error
                assert payload(result)["returned"] == 1

    anyio.run(go)


def test_mcp_subcommand_without_sdk_gives_install_hint(
    populated: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]  # noqa: F811
) -> None:
    monkeypatch.setitem(sys.modules, "jobpoller.mcp_server", None)  # makes the import fail
    assert main(["mcp", "--config", str(populated)], now=NOW) == 1
    assert "jobpoller[mcp]" in capsys.readouterr().err
