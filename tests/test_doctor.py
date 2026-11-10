from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest

from jobpoller import doctor
from jobpoller.cli import main
from jobpoller.doctor import CheckResult, run_checks
from tests.fixtures import write_config


def by_name(results: list[CheckResult]) -> dict[str, CheckResult]:
    return {r.name: r for r in results}


def ollama(models: list[str] | None = None, size: int = 10_000, vram: int = 10_000,
           down: bool = False) -> httpx.MockTransport:
    names = models if models is not None else ["qwen3:14b"]

    def handler(request: httpx.Request) -> httpx.Response:
        if down:
            raise httpx.ConnectError("refused", request=request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in names]})
        if request.url.path == "/api/chat":
            return httpx.Response(200, json={"message": {"content": "."}})
        if request.url.path == "/api/ps":
            entry = {"name": "qwen3:14b", "size": size, "size_vram": vram}
            return httpx.Response(200, json={"models": [entry]})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def fake_systemctl(installed: bool = False):  # type: ignore[no-untyped-def]
    def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["systemctl", "--user", "show"] and installed:
            out = "LoadState=loaded\nUnitFileState=enabled\nActiveState=active\n"
        elif cmd[:3] == ["systemctl", "--user", "show"]:
            out = "LoadState=not-found\n"
        else:
            out = ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    return run


@pytest.fixture(autouse=True)
def notify_send(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jobpoller.doctor.shutil.which", lambda _: "/usr/bin/notify-send")


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    path = write_config(tmp_path, llm=True)
    path.write_text(path.read_text() + "queue_path: queue.md\n")
    return path


def test_everything_ok(cfg: Path) -> None:
    results = by_name(run_checks(cfg, ollama(), runner=fake_systemctl(installed=True)))
    assert all(r.ok for r in results.values()), [r for r in results.values() if not r.ok]
    assert "100% on GPU" in results["gpu"].detail


def test_each_failure_has_a_fix_hint(cfg: Path) -> None:
    (cfg.parent / "resume.md").unlink()
    results = by_name(run_checks(cfg, ollama(models=["llama3:8b"]), runner=fake_systemctl()))
    assert not results["resume"].ok and "llm.resume" in results["resume"].fix
    assert results["model"].fix == "ollama pull qwen3:14b"
    assert "gpu" not in results  # nothing to load
    assert results["scheduler"].fix == "run `jobpoller schedule install`"


def test_ollama_down_stops_llm_checks(cfg: Path) -> None:
    results = by_name(run_checks(cfg, ollama(down=True), runner=fake_systemctl()))
    assert not results["ollama"].ok and "systemctl start ollama" in results["ollama"].fix
    assert "model" not in results and "gpu" not in results


def test_gpu_cpu_only_and_partial(cfg: Path) -> None:
    cpu = by_name(run_checks(cfg, ollama(vram=0), runner=fake_systemctl()))["gpu"]
    assert not cpu.ok and "CPU" in cpu.detail and "ROCm" in cpu.fix
    part = by_name(run_checks(cfg, ollama(vram=6_000), runner=fake_systemctl()))["gpu"]
    assert not part.ok and "60%" in part.detail and "num_ctx" in part.fix
    skipped = by_name(run_checks(cfg, ollama(vram=0), gpu=False, runner=fake_systemctl()))
    assert "gpu" not in skipped


def test_queue_notify_and_llm_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_config(tmp_path, llm=False)
    results = by_name(run_checks(path, None, runner=fake_systemctl()))
    assert results["llm"].ok and "disabled" in results["llm"].detail
    assert not results["queue"].ok and "queue_path" in results["queue"].fix
    path.write_text(path.read_text() + "queue_path: nope/q.md\n")
    queue = by_name(run_checks(path, None, runner=fake_systemctl()))["queue"]
    assert "folder does not exist" in queue.detail
    monkeypatch.setattr("jobpoller.doctor.shutil.which", lambda _: None)
    notify = by_name(run_checks(path, None, runner=fake_systemctl()))["notify"]
    assert "libnotify" in notify.fix


def test_missing_and_broken_config(tmp_path: Path) -> None:
    missing = run_checks(tmp_path / "config.yaml", None, runner=fake_systemctl())
    assert missing[0].name == "config" and not missing[0].ok and "jobpoller init" in missing[0].fix
    bad = tmp_path / "bad.yaml"
    bad.write_text("boards:\n  workday: [x]\n")
    assert not run_checks(bad, None, runner=fake_systemctl())[0].ok


def test_init_creates_config_from_example_then_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    example = tmp_path / "example.yaml"
    example.write_text("hn: false\n")
    monkeypatch.setattr(doctor, "EXAMPLE_CONFIG", example)
    monkeypatch.setattr("jobpoller.doctor.run_systemctl", fake_systemctl())
    target = tmp_path / "new" / "config.yaml"
    target.parent.mkdir()
    code = main(["init", "--yes", "--no-gpu", "--config", str(target)])
    out = capsys.readouterr().out
    assert target.read_text() == "hn: false\n" and "Created" in out
    assert code == 1 and "FAIL  scheduler" in out and "fix: run `jobpoller schedule install`" in out


def test_init_does_not_prompt_or_create_when_not_interactive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("jobpoller.doctor.run_systemctl", fake_systemctl())
    target = tmp_path / "config.yaml"
    code = main(["init", "--no-gpu", "--config", str(target)])  # stdin is not a tty under pytest
    assert code == 1
    assert not target.exists() and "Skipped" in capsys.readouterr().out
