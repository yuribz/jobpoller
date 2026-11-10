from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jobpoller.cli import main
from jobpoller.config import ConfigError, load_config
from jobpoller.schedule import (
    ScheduleError,
    apply_install,
    normalize_time,
    plan_install,
    remove,
    render_service,
    render_timer,
    status,
)
from tests.fixtures import write_config


class FakeSystemctl:
    """Records calls; `props` maps a unit to the `systemctl show` output to return."""

    def __init__(self, props: dict[str, str] | None = None, fail: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.props = props or {}
        self.fail = fail

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if self.fail and self.fail in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "Failed to enable unit: boom")
        out = ""
        if cmd[:3] == ["systemctl", "--user", "show"]:
            out = self.props.get(cmd[3], "")
        if cmd[0] == "loginctl":
            out = "Linger=no\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")


def test_time_normalization() -> None:
    assert normalize_time("8:05") == "08:05"
    assert normalize_time("23:59") == "23:59"
    assert normalize_time(485) == "08:05"  # YAML turns an unquoted 8:05 into the int 485
    for bad in ["25:00", "8", "08:60", "noon", True, 24 * 60]:
        with pytest.raises(ScheduleError):
            normalize_time(bad)


def test_service_quotes_paths_with_spaces_and_specials() -> None:
    py = Path("/mnt/My Projects/jobpoller/.venv/bin/python")
    cfg = Path("/mnt/My Projects/job 100%/config.yaml")
    unit = render_service(py, cfg)
    assert 'ExecStart="/mnt/My Projects/jobpoller/.venv/bin/python" -m jobpoller run' in unit
    assert '--config "/mnt/My Projects/job 100%%/config.yaml"' in unit  # % is escaped
    assert "WorkingDirectory=/mnt/My Projects/job 100%%" in unit
    assert "Type=oneshot" in unit


def test_timer_is_daily_and_persistent() -> None:
    timer = render_timer("7:30")
    assert "OnCalendar=*-*-* 07:30:00" in timer
    assert "Persistent=true" in timer and "WantedBy=timers.target" in timer


def test_install_writes_units_then_reloads_and_enables(tmp_path: Path) -> None:
    fake = FakeSystemctl()
    plan = plan_install(Path("/venv/bin/python"), Path("/p/config.yaml"), "08:00", tmp_path / "u")
    assert not (tmp_path / "u").exists()  # planning has no side effects
    assert any("enable --now jobpoller.timer" in line for line in plan.describe())
    apply_install(plan, fake)
    assert (tmp_path / "u" / "jobpoller.service").read_text() == plan.service_text
    assert (tmp_path / "u" / "jobpoller.timer").read_text() == plan.timer_text
    assert fake.calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "jobpoller.timer"],
    ]
    apply_install(plan, FakeSystemctl())  # reinstalling is fine (idempotent)


def test_failed_systemctl_raises_with_its_message(tmp_path: Path) -> None:
    plan = plan_install(Path("/p/python"), Path("/p/c.yaml"), "08:00", tmp_path)
    with pytest.raises(ScheduleError, match="boom"):
        apply_install(plan, FakeSystemctl(fail="enable"))


def test_remove_deletes_units_and_is_a_noop_when_absent(tmp_path: Path) -> None:
    plan = plan_install(Path("/p/python"), Path("/p/c.yaml"), "08:00", tmp_path)
    apply_install(plan, FakeSystemctl())
    fake = FakeSystemctl()
    assert remove(tmp_path, fake) is True
    assert list(tmp_path.iterdir()) == []
    assert ["systemctl", "--user", "disable", "--now", "jobpoller.timer"] in fake.calls
    assert fake.calls[-1] == ["systemctl", "--user", "daemon-reload"]
    assert remove(tmp_path, FakeSystemctl()) is False


def test_status_parses_systemctl_output() -> None:
    fake = FakeSystemctl(
        {
            "jobpoller.timer": "LoadState=loaded\nUnitFileState=enabled\nActiveState=active\n"
            "NextElapseUSecRealtime=Tue 2026-11-10 08:00:00 PST\n",
            "jobpoller.service": "Result=success\n"
            "ExecMainExitTimestamp=Mon 2026-11-09 08:00:03 PST\n",
        }
    )
    s = status(fake)
    assert (s.installed, s.enabled, s.active, s.last_result) == (True, True, True, "success")
    assert "2026-11-10 08:00" in s.next_run and s.linger is False

    absent = status(FakeSystemctl({"jobpoller.timer": "LoadState=not-found\n"}))
    assert not absent.installed
    fresh = status(FakeSystemctl({"jobpoller.timer": "LoadState=loaded\nUnitFileState=enabled\n"}))
    assert fresh.last_result == "never run"


def test_schedule_time_in_config(tmp_path: Path) -> None:
    path = write_config(tmp_path, llm=False)
    assert load_config(path).schedule_time == "08:00"
    path.write_text(path.read_text() + "schedule_time: 7:15\n")  # unquoted: YAML int
    assert load_config(path).schedule_time == "07:15"
    path.write_text(path.read_text() + "schedule_time: 'late'\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_cli_dry_run_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    path = write_config(tmp_path, llm=False)
    assert main(["schedule", "install", "--dry-run", "--time", "6:45", "--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "06:45" in out and "daemon-reload" in out and "nothing changed" in out
    assert str(path.resolve()) in out
    assert not (tmp_path / "xdg").exists()
    assert main(["schedule", "install", "--dry-run", "--time", "99:00", "--config", str(path)]) == 1
