"""Manage a systemd *user* timer that runs `jobpoller run` daily.

Rendering is pure (testable); every systemctl call goes through an injectable runner.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

UNIT = "jobpoller"
Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class ScheduleError(Exception):
    pass


def normalize_time(value: object) -> str:
    """'8:05' -> '08:05'. Also accepts the int YAML produces for an unquoted 8:05 (minutes)."""
    if isinstance(value, bool):
        raise ScheduleError(f"invalid time {value!r}; use HH:MM, e.g. 08:00")
    if isinstance(value, int):  # YAML 1.1 reads unquoted 8:05 as sexagesimal 485
        hours, minutes = divmod(value, 60)
    else:
        match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value))
        if match is None:
            raise ScheduleError(f"invalid time {value!r}; use HH:MM, e.g. 08:00")
        hours, minutes = int(match.group(1)), int(match.group(2))
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise ScheduleError(f"invalid time {value!r}; use HH:MM, e.g. 08:00")
    return f"{hours:02d}:{minutes:02d}"


def _exec_arg(arg: str) -> str:
    """Quote for ExecStart=: handles spaces, and the % and $ that systemd would expand."""
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def render_service(python: Path, config: Path) -> str:
    return (
        "[Unit]\n"
        "Description=jobpoller: fetch job boards and queue new matches\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={str(config.parent).replace('%', '%%')}\n"
        f"ExecStart={_exec_arg(str(python))} -m jobpoller run --config {_exec_arg(str(config))}\n"
        "TimeoutStartSec=30min\n"
    )


def render_timer(time: str) -> str:
    return (
        "[Unit]\n"
        "Description=Run jobpoller daily\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar=*-*-* {normalize_time(time)}:00\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def default_unit_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user"


def run_systemctl(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except FileNotFoundError as exc:
        raise ScheduleError(f"{cmd[0]} not found; a systemd user session is required") from exc
    except subprocess.TimeoutExpired as exc:
        raise ScheduleError(f"{' '.join(cmd)} timed out") from exc


@dataclass(frozen=True)
class Plan:
    service_path: Path
    timer_path: Path
    service_text: str
    timer_text: str
    commands: tuple[tuple[str, ...], ...]

    def describe(self) -> list[str]:
        lines = [f"write {self.service_path}", f"write {self.timer_path}"]
        lines += [" ".join(c) for c in self.commands]
        return lines


def plan_install(
    python: Path, config: Path, time: str, unit_dir: Path | None = None, name: str = UNIT
) -> Plan:
    unit_dir = unit_dir or default_unit_dir()
    return Plan(
        service_path=unit_dir / f"{name}.service",
        timer_path=unit_dir / f"{name}.timer",
        service_text=render_service(python, config),
        timer_text=render_timer(time),
        commands=(
            ("systemctl", "--user", "daemon-reload"),
            ("systemctl", "--user", "enable", "--now", f"{name}.timer"),
        ),
    )


def _must(runner: Runner, cmd: list[str]) -> None:
    done = runner(cmd)
    if done.returncode != 0:
        raise ScheduleError(f"`{' '.join(cmd)}` failed: {(done.stderr or done.stdout).strip()}")


def apply_install(plan: Plan, runner: Runner = run_systemctl) -> None:
    plan.service_path.parent.mkdir(parents=True, exist_ok=True)
    plan.service_path.write_text(plan.service_text, encoding="utf-8")
    plan.timer_path.write_text(plan.timer_text, encoding="utf-8")
    for cmd in plan.commands:
        _must(runner, list(cmd))


def remove(unit_dir: Path | None = None, runner: Runner = run_systemctl, name: str = UNIT) -> bool:
    """Disable the timer and delete the unit files; False if nothing was installed."""
    unit_dir = unit_dir or default_unit_dir()
    paths = [unit_dir / f"{name}.timer", unit_dir / f"{name}.service"]
    present = [p for p in paths if p.exists()]
    if not present:
        return False
    runner(["systemctl", "--user", "disable", "--now", f"{name}.timer"])  # ok if already gone
    for path in present:
        path.unlink()
    _must(runner, ["systemctl", "--user", "daemon-reload"])
    return True


@dataclass(frozen=True)
class Status:
    installed: bool
    enabled: bool
    active: bool
    next_run: str
    last_result: str  # e.g. "success", "exit-code", or "never run"
    linger: bool | None  # None if unknown


def _show(runner: Runner, unit: str, props: list[str]) -> dict[str, str]:
    done = runner(["systemctl", "--user", "show", unit, f"--property={','.join(props)}"])
    pairs = (line.partition("=") for line in done.stdout.splitlines() if "=" in line)
    return {k: v for k, _, v in pairs}


def status(runner: Runner = run_systemctl, name: str = UNIT) -> Status:
    timer = _show(
        runner,
        f"{name}.timer",
        ["LoadState", "UnitFileState", "ActiveState", "NextElapseUSecRealtime"],
    )
    installed = timer.get("LoadState", "not-found") != "not-found"
    service = (
        _show(runner, f"{name}.service", ["Result", "ExecMainExitTimestamp"]) if installed else {}
    )
    ran = bool(service.get("ExecMainExitTimestamp"))
    return Status(
        installed=installed,
        enabled=timer.get("UnitFileState") == "enabled",
        active=timer.get("ActiveState") == "active",
        next_run=timer.get("NextElapseUSecRealtime") or "-",
        last_result=(service.get("Result") or "unknown") if ran else "never run",
        linger=_linger(runner),
    )


def _linger(runner: Runner) -> bool | None:
    try:
        done = runner(["loginctl", "show-user", str(os.getuid()), "--property=Linger"])
    except ScheduleError:
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip() == "Linger=yes"
