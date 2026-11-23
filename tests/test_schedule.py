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
    python_candidates,
    remove,
    render_failure_service,
    render_launcher,
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


def test_service_runs_the_launcher_and_quotes_specials() -> None:
    launcher = Path("/home/u/.config/systemd/user/jobpoller-run.sh")
    unit = render_service(launcher, Path("/mnt/My Projects/job 100%"), "jobpoller-failed.service")
    assert f'ExecStart=/bin/sh "{launcher}"' in unit
    assert "WorkingDirectory=/mnt/My Projects/job 100%%" in unit  # % is escaped
    assert "OnFailure=jobpoller-failed.service" in unit and "Type=oneshot" in unit
    assert "OnFailure" not in render_service(launcher, Path("/p"))


def test_candidates_prefer_installer_python_then_project_venv_then_path() -> None:
    cfg = Path("/mnt/My Projects/jobpoller/config.yaml")
    assert python_candidates(Path("/other/python"), cfg) == [
        "/other/python", "/mnt/My Projects/jobpoller/.venv/bin/python", "python3",
    ]  # fmt: skip
    venv = Path("/mnt/My Projects/jobpoller/.venv/bin/python")
    assert python_candidates(venv, cfg) == [str(venv), "python3"]  # no duplicate


def _fake_python(path: Path, works: bool) -> str:
    """A stand-in interpreter: `-c` (the import probe) succeeds or fails; otherwise record argv."""
    probe = "exit 0" if works else "exit 1"
    path.write_text(
        f'#!/bin/sh\n[ "$1" = "-c" ] && {{ {probe}; }}\necho "$@" > "{path}.ran"\n'
    )
    path.chmod(0o755)
    return str(path)


def test_launcher_skips_broken_pythons_and_runs_the_first_working_one(tmp_path: Path) -> None:
    cfg = tmp_path / "my config.yaml"
    bad = _fake_python(tmp_path / "bad", works=False)
    good = _fake_python(tmp_path / "good space", works=True)
    script = tmp_path / "run.sh"
    script.write_text(render_launcher([bad, str(tmp_path / "missing"), good, "python3"], cfg))
    done = subprocess.run(["/bin/sh", str(script)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert f"skipping {bad}" in done.stderr
    assert (tmp_path / "good space.ran").read_text().strip() == f"-m jobpoller run --config {cfg}"
    assert not (tmp_path / "bad.ran").exists()


def test_launcher_exits_127_when_no_python_works(tmp_path: Path) -> None:
    script = tmp_path / "run.sh"
    script.write_text(
        render_launcher([_fake_python(tmp_path / "bad", works=False)], tmp_path / "c.yaml")
    )
    done = subprocess.run(["/bin/sh", str(script)], capture_output=True, text=True)
    assert done.returncode == 127 and "no working Python found" in done.stderr


def test_failure_unit_uses_curl_not_python() -> None:
    unit = render_failure_service(Path("/usr/bin/curl"), "https://ntfy.example/my%topic")
    assert 'ExecStart="/usr/bin/curl"' in unit
    assert '"https://ntfy.example/my%%topic"' in unit  # % escaped for systemd
    assert '"Title: jobpoller run failed"' in unit and '"Priority: high"' in unit
    assert "--fail" in unit and "python" not in unit.lower()


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


def test_install_writes_executable_launcher_and_failure_unit(tmp_path: Path) -> None:
    plan = plan_install(
        Path("/venv/bin/python"), Path("/p/config.yaml"), "08:00", tmp_path / "u",
        failure_ntfy="https://ntfy.example/t", curl=Path("/usr/bin/curl"),
    )  # fmt: skip
    assert any("jobpoller-failed.service" in line for line in plan.describe())
    apply_install(plan, FakeSystemctl())
    launcher = tmp_path / "u" / "jobpoller-run.sh"
    assert launcher.stat().st_mode & 0o111 and launcher.read_text() == plan.launcher_text
    failed = (tmp_path / "u" / "jobpoller-failed.service").read_text()
    assert "https://ntfy.example/t" in failed
    main_unit = (tmp_path / "u" / "jobpoller.service").read_text()
    assert "OnFailure=jobpoller-failed.service" in main_unit


def test_no_failure_unit_without_ntfy_and_missing_curl_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = plan_install(Path("/p/python"), Path("/p/c.yaml"), "08:00", tmp_path)
    assert plan.failure_path is None and "OnFailure" not in plan.service_text
    apply_install(plan, FakeSystemctl())
    assert not (tmp_path / "jobpoller-failed.service").exists()
    monkeypatch.setattr("shutil.which", lambda _name: None)
    with pytest.raises(ScheduleError, match="curl not found"):
        plan_install(Path("/p/python"), Path("/p/c.yaml"), "08:00", tmp_path, failure_ntfy="https://n/t")


def test_failed_systemctl_raises_with_its_message(tmp_path: Path) -> None:
    plan = plan_install(Path("/p/python"), Path("/p/c.yaml"), "08:00", tmp_path)
    with pytest.raises(ScheduleError, match="boom"):
        apply_install(plan, FakeSystemctl(fail="enable"))


def test_remove_deletes_units_and_is_a_noop_when_absent(tmp_path: Path) -> None:
    plan = plan_install(Path("/p/python"), Path("/p/c.yaml"), "08:00", tmp_path)
    apply_install(plan, FakeSystemctl())
    fake = FakeSystemctl()
    assert remove(tmp_path, fake) is True
    assert list(tmp_path.iterdir()) == []  # service, timer, launcher
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
