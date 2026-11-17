"""`jobpoller chat`: the Ollama tool-calling loop, scripted offline."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from jobpoller.chat import SYSTEM_PROMPT, repl, run_turn, strip_think, trim_history
from jobpoller.cli import main
from jobpoller.config import load_config
from jobpoller.ranker import LLMUnavailable
from jobpoller.tools import ToolContext
from tests.test_workflow import populated  # noqa: F401  (fixture)

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def call(name: str, **args: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": name, "arguments": args}}
    ]}  # fmt: skip


def say(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


class ScriptedOllama:
    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        return httpx.Response(200, json={"message": self.replies.pop(0)})


def setup_turn(path: Path, replies: list[dict[str, Any]], **kw: Any):  # type: ignore[no-untyped-def]
    config = load_config(path)
    fake = ScriptedOllama(replies)
    client = httpx.Client(transport=httpx.MockTransport(fake))
    ctx = ToolContext(config, now=lambda: NOW, **kw)
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT},
                                      {"role": "user", "content": "triage"}]  # fmt: skip
    return config, fake, client, ctx, messages


def test_tool_loop_runs_list_then_answers(populated: Path) -> None:  # noqa: F811
    config, fake, client, ctx, messages = setup_turn(
        populated, [call("list_jobs", limit=2), say("<think>hm</think>Top two listed.")]
    )
    out = run_turn(client, config.llm, ctx, messages)
    assert out == "Top two listed."
    tool_msg = next(m for m in messages if m["role"] == "tool")
    assert tool_msg["tool_name"] == "list_jobs" and json.loads(tool_msg["content"])["returned"] == 2
    first = fake.requests[0]
    assert first["think"] is False and first["options"]["num_ctx"] == config.llm.num_ctx
    offered = {t["function"]["name"] for t in first["tools"]}
    assert offered >= {"list_jobs", "dismiss", "add_posting"}


def test_mutating_call_needs_confirmation(populated: Path) -> None:  # noqa: F811
    config, _, client, ctx, messages = setup_turn(populated, [call("dismiss", ids=[1]), say("no")])
    run_turn(client, config.llm, ctx, messages, confirm=lambda n, a: False)
    result = json.loads(next(m for m in messages if m["role"] == "tool")["content"])
    assert result == {"error": "the user declined this action"}
    config, _, client, ctx, messages = setup_turn(populated, [call("dismiss", ids=[1]), say("ok")])
    run_turn(client, config.llm, ctx, messages, confirm=lambda n, a: True)
    assert json.loads(next(m for m in messages if m["role"] == "tool")["content"])["changed"] == [1]


def test_read_only_hides_mutating_tools_and_blocks_calls(populated: Path) -> None:  # noqa: F811
    config, fake, client, ctx, messages = setup_turn(
        populated, [call("dismiss", ids=[1]), say("done")], read_only=True
    )
    run_turn(client, config.llm, ctx, messages)
    assert {t["function"]["name"] for t in fake.requests[0]["tools"]} == {"list_jobs", "get_job"}
    assert "read-only" in json.loads(next(m for m in messages if m["role"] == "tool")["content"])[
        "error"
    ]


def test_bad_arguments_are_fed_back_not_raised(populated: Path) -> None:  # noqa: F811
    config, _, client, ctx, messages = setup_turn(
        populated, [call("get_job", id="abc"), call("nonsense"), say("sorry")]
    )
    assert run_turn(client, config.llm, ctx, messages) == "sorry"
    errors = [json.loads(m["content"])["error"] for m in messages if m["role"] == "tool"]
    assert "integer" in errors[0] and "unknown tool" in errors[1]


def test_stops_after_max_rounds(populated: Path) -> None:  # noqa: F811
    config, _, client, ctx, messages = setup_turn(populated, [call("list_jobs")] * 3)
    out = run_turn(client, config.llm, ctx, messages, max_rounds=3)
    assert "too many tool rounds" in out


def test_string_arguments_are_parsed(populated: Path) -> None:  # noqa: F811
    reply = {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "get_job", "arguments": '{"id": 1}'}}]}  # fmt: skip
    config, _, client, ctx, messages = setup_turn(populated, [reply, say("ok")])
    run_turn(client, config.llm, ctx, messages)
    assert json.loads(next(m for m in messages if m["role"] == "tool")["content"])["id"] == 1


def test_ollama_down_raises_llm_unavailable(populated: Path) -> None:  # noqa: F811
    config = load_config(populated)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(boom))
    ctx = ToolContext(config)
    try:
        run_turn(client, config.llm, ctx, [{"role": "system", "content": "s"},
                                           {"role": "user", "content": "hi"}])  # fmt: skip
    except LLMUnavailable as exc:
        assert "Ollama" in str(exc)
    else:
        raise AssertionError("expected LLMUnavailable")


def test_trim_history_keeps_system_and_latest_turn_and_never_starts_on_tool() -> None:
    big = "x" * 3000
    msgs: list[dict[str, Any]] = [{"role": "system", "content": "sys"}]
    for i in range(10):
        msgs += [{"role": "user", "content": f"q{i}"}, call("list_jobs"),
                 {"role": "tool", "tool_name": "list_jobs", "content": big},
                 say(f"a{i}")]  # fmt: skip
    msgs.append({"role": "user", "content": "latest"})
    out = trim_history(msgs, num_ctx=4096, reserve_tokens=1024)
    assert out[0]["role"] == "system" and out[-1]["content"] == "latest"
    assert out[1]["role"] == "user" and len(out) < len(msgs)


def test_strip_think() -> None:
    assert strip_think("<think>a\nb</think>\nhello ") == "hello"


def test_repl_end_to_end_and_ollama_down_message(populated: Path) -> None:  # noqa: F811
    config = load_config(populated)
    fake = ScriptedOllama([call("list_jobs", limit=1), say("Here is the best one.")])
    lines = iter(["what is new?", ""])
    out: list[str] = []
    ctx = ToolContext(config, now=lambda: NOW)
    rc = repl(config, ctx, transport=httpx.MockTransport(fake),
              input_fn=lambda _p: next(lines), print_fn=out.append)  # fmt: skip
    assert rc == 0 and any("Here is the best one." in line for line in out)
    assert any("list_jobs" in line for line in out)  # tool calls are shown

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    lines = iter(["hello", ""])
    out = []
    repl(config, ctx, transport=httpx.MockTransport(down),
         input_fn=lambda _p: next(lines), print_fn=out.append)  # fmt: skip
    assert any("ollama serve" in line for line in out)


def test_chat_subcommand_is_wired(
    populated: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    fake = ScriptedOllama([say("hi")])
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\n\n"))
    rc = main(["chat", "--config", str(populated), "--model", "other:1b"],
              transport=httpx.MockTransport(fake), now=NOW)  # fmt: skip
    assert rc == 0 and fake.requests[0]["model"] == "other:1b"
