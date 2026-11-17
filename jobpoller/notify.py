"""Best-effort notifications after a run. Failing to notify must never fail the run."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import httpx

from .models import Match


@dataclass
class NotifyConfig:
    threshold: int = 80  # notify when any new job has LLM fit >= this
    desktop: bool = True  # notify-send
    ntfy: str = ""  # full topic URL, e.g. https://ntfy.sh/my-private-topic; empty = off


Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


def summarize(matches: Sequence[Match], threshold: int) -> tuple[str, str, str] | None:
    """(title, body, url of the best job) or None. Keyword-only jobs have no fit: skipped."""
    strong = [m for m in matches if m.ranking is not None and m.ranking.fit >= threshold]
    if not strong:
        return None
    best = max(strong, key=lambda m: m.sort_key)
    plural = "match" if len(strong) == 1 else "matches"
    title = f"jobpoller: {len(strong)} strong {plural}"
    fit = best.ranking.fit if best.ranking else 0
    return title, f"Top: {best.job.title[:80]} @ {best.job.company} (fit {fit})", best.job.url


def notify(
    cfg: NotifyConfig,
    matches: Sequence[Match],
    client: httpx.Client,
    runner: Runner | None = None,
    status: str = "",
) -> list[str]:
    """Send notifications; returns warnings instead of raising.

    With no strong match, ntfy (only) gets a low-priority heartbeat carrying `status`, so a
    silent day is distinguishable from a run that never happened.
    """
    message = summarize(matches, cfg.threshold)
    warnings: list[str] = []
    try:
        if message is None:
            if cfg.ntfy:
                title = f"jobpoller: no fit >= {cfg.threshold} today"
                body = status or "run completed"
                warnings += _ntfy(cfg.ntfy, title, body, "", client, quiet=True)
            return warnings
        title, body, url = message
        if status:
            body = f"{body}\n{status}"
        if cfg.desktop:
            warnings += _desktop(title, body, runner or subprocess.run)
        if cfg.ntfy:
            warnings += _ntfy(cfg.ntfy, title, body, url, client)
    except Exception as exc:  # noqa: BLE001 - contract: a notification bug never fails a run
        warnings.append(f"notify: unexpected error: {exc!r}")
    return warnings


def _desktop(title: str, body: str, runner: Runner) -> list[str]:
    if shutil.which("notify-send") is None:
        return ["notify: notify-send not found (install libnotify) or set notify.desktop: false"]
    try:
        done = runner(
            ["notify-send", "--app-name=jobpoller", title, body],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"notify: notify-send failed: {exc!r}"]
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()[:200]
        return [f"notify: notify-send exited {done.returncode}: {detail}"]
    return []


def _ntfy(
    topic: str, title: str, body: str, link: str, client: httpx.Client, quiet: bool = False
) -> list[str]:
    # HTTP headers are latin-1; keep the title safe and put full text in the body.
    headers = {"Title": title.encode("ascii", "replace").decode("ascii")}
    if quiet:
        headers["Priority"] = "low"  # heartbeat: no sound or vibration
    if link.startswith(("http://", "https://")) and link.isascii():
        headers["Click"] = link  # tapping the push opens the posting
    try:
        response = client.post(topic, content=body.encode("utf-8"), headers=headers, timeout=10.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return [f"notify: ntfy failed: {exc}"]
    return []
