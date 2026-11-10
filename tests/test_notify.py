from __future__ import annotations

import subprocess
from typing import Any

import httpx
import pytest

from jobpoller.models import Match, Ranking
from jobpoller.notify import NotifyConfig, notify, summarize
from tests.test_scoring import make


def match(n: int, fit: int | None) -> Match:
    ranking = Ranking(fit) if fit is not None else None
    return Match(make(job_id=str(n), title=f"Role {n}", company="acme"), 10, ranking)


class FakeRun:
    def __init__(self, code: int = 0, exc: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self.code, self.exc = code, exc

    def __call__(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(cmd)
        assert kw["timeout"] == 5
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.code, b"", b"boom")


@pytest.fixture(autouse=True)
def has_notify_send(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jobpoller.notify.shutil.which", lambda _: "/usr/bin/notify-send")


def client(handler: Any = lambda r: httpx.Response(200)) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_summary_counts_only_strong_ranked_matches() -> None:
    assert summarize([match(1, 79), match(2, None)], 80) is None
    title, body, _ = summarize([match(1, 85), match(2, 92), match(3, 60)], 80) or ("", "", "")
    assert title == "jobpoller: 2 strong matches"
    assert "Role 2" in body and "fit 92" in body
    assert (summarize([match(1, 80)], 80) or ("",))[0] == "jobpoller: 1 strong match"


def test_desktop_notification_sent_once_with_summary() -> None:
    run = FakeRun()
    assert notify(NotifyConfig(), [match(1, 85), match(2, 90)], client(), run) == []
    assert len(run.calls) == 1
    assert run.calls[0][0] == "notify-send"
    assert run.calls[0][2] == "jobpoller: 2 strong matches" and "Role 2" in run.calls[0][3]


def test_below_threshold_sends_nothing() -> None:
    run = FakeRun()
    assert notify(NotifyConfig(), [match(1, 79)], client(), run) == []
    assert run.calls == []


@pytest.mark.parametrize(
    "run",
    [FakeRun(code=1), FakeRun(exc=FileNotFoundError("x")),
     FakeRun(exc=subprocess.TimeoutExpired("notify-send", 5)), FakeRun(exc=RuntimeError("?"))],
)
def test_desktop_failures_become_warnings(run: FakeRun) -> None:
    warnings = notify(NotifyConfig(), [match(1, 90)], client(), run)
    assert len(warnings) == 1 and warnings[0].startswith("notify:")


def test_missing_notify_send_is_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jobpoller.notify.shutil.which", lambda _: None)
    warnings = notify(NotifyConfig(), [match(1, 90)], client(), FakeRun())
    assert "notify-send not found" in warnings[0]


def test_ntfy_posts_summary_and_failures_are_warnings() -> None:
    seen: list[httpx.Request] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    cfg = NotifyConfig(desktop=False, ntfy="https://ntfy.example/topic")
    assert notify(cfg, [match(1, 95)], client(ok)) == []
    assert str(seen[0].url) == "https://ntfy.example/topic"
    assert seen[0].headers["Title"] == "jobpoller: 1 strong match"
    assert b"Role 1" in seen[0].content
    assert seen[0].headers["Click"] == match(1, 95).job.url

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    assert "ntfy failed" in notify(cfg, [match(1, 95)], client(down))[0]
    assert "ntfy failed" in notify(cfg, [match(1, 95)], client(lambda r: httpx.Response(500)))[0]
