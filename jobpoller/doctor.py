"""Setup checklist shared by `jobpoller init` (and later the TUI).

Every check returns a result with a concrete fix hint; none of them raises.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import PROFILES, Config, ConfigError, load_config
from .schedule import Runner, ScheduleError, run_systemctl, status

EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "config.example.yaml"


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    fix: str = ""


def create_config_from_example(path: Path, profile: str | None = None) -> None:
    if path.exists():
        raise FileExistsError(path)
    if not EXAMPLE_CONFIG.is_file():
        raise FileNotFoundError(
            f"{EXAMPLE_CONFIG} not found (jobpoller was not installed from a source checkout)"
        )
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    if profile is not None:
        if profile not in PROFILES:
            raise ValueError(f"profile must be one of {sorted(PROFILES)}")
        text = text.replace("  profile: heavy\n", f"  profile: {profile}\n", 1)
    path.write_text(text, encoding="utf-8")


def _has_model(names: set[str], model: str) -> bool:
    return model in names or (":" not in model and f"{model}:latest" in names)


def _llm_checks(
    config: Config, transport: httpx.BaseTransport | None, gpu: bool
) -> list[CheckResult]:
    llm = config.llm
    results: list[CheckResult] = []

    resume = llm.resume_path
    if resume is None or not resume.is_file():
        results.append(
            CheckResult(
                "resume",
                False,
                f"not found: {resume}" if resume else "llm.resume is not set",
                "save your resume as plain .md/.txt and point llm.resume at it "
                "(or set llm.enabled: false for keyword-only runs)",
            )
        )
    elif not resume.read_text(encoding="utf-8").strip():
        results.append(
            CheckResult("resume", False, f"{resume} is empty", "paste your resume into it")
        )
    else:
        results.append(CheckResult("resume", True, str(resume)))

    with httpx.Client(timeout=llm.timeout_seconds, transport=transport) as client:
        try:
            response = client.get(f"{llm.base_url}/api/tags")
            response.raise_for_status()
            names = {str(m.get("name")) for m in response.json().get("models", [])}
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            results.append(
                CheckResult(
                    "ollama",
                    False,
                    f"cannot reach {llm.base_url}: {exc}",
                    "start it with `systemctl start ollama` (or `ollama serve`)",
                )
            )
            return results
        results.append(CheckResult("ollama", True, f"reachable at {llm.base_url}"))

        if not _has_model(names, llm.model):
            results.append(
                CheckResult(
                    "model", False, f"'{llm.model}' is not pulled", f"ollama pull {llm.model}"
                )
            )
            return results
        results.append(CheckResult("model", True, f"{llm.model} is pulled"))
        if gpu and llm.profile == "heavy":
            results.append(_gpu_check(client, config))
        elif gpu:
            results.append(
                CheckResult(
                    "gpu",
                    True,
                    f"skipped: profile '{llm.profile}' is meant for CPU / integrated graphics",
                )
            )
    return results


def _gpu_check(client: httpx.Client, config: Config) -> CheckResult:
    llm = config.llm
    fix = (
        "likely causes: Ollama lacks the ROCm build, or its user cannot open /dev/kfd and "
        "/dev/dri (needs the render/video groups). Look for GPU lines in "
        "`journalctl -u ollama -b`"
    )
    try:
        client.post(
            f"{llm.base_url}/api/chat",
            json={
                "model": llm.model,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                "think": False,
                "keep_alive": llm.keep_alive,
                "options": {"num_predict": 1, "num_ctx": llm.num_ctx},
            },
        ).raise_for_status()  # loads the model so /api/ps has something to report
        loaded = client.get(f"{llm.base_url}/api/ps").json().get("models", [])
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        return CheckResult("gpu", False, f"could not load {llm.model}: {exc}", "")
    entry = next((m for m in loaded if _has_model({str(m.get("name"))}, llm.model)), None)
    if entry is None or not entry.get("size"):
        return CheckResult("gpu", False, "model is not listed in /api/ps after loading", "")
    share = int(entry.get("size_vram", 0)) / int(entry["size"])
    gb = int(entry["size"]) / 1e9
    if share >= 0.99:
        return CheckResult("gpu", True, f"{llm.model} is 100% on GPU ({gb:.1f} GB)")
    if share == 0:
        return CheckResult("gpu", False, f"{llm.model} is running on the CPU (slow)", fix)
    return CheckResult(
        "gpu",
        False,
        f"only {share:.0%} of {llm.model} fits in VRAM (partial offload is slow)",
        "lower llm.num_ctx or use a smaller model; " + fix,
    )


def _queue_check(config: Config) -> CheckResult:
    path = config.queue_path
    if path is None:
        return CheckResult(
            "queue",
            False,
            "queue_path is not set (promote is disabled)",
            "add `queue_path: path/to/your/queue.md` to config.yaml",
        )
    if not path.parent.is_dir():
        return CheckResult(
            "queue", False, f"folder does not exist: {path.parent}", "create it or fix queue_path"
        )
    if not os.access(path if path.exists() else path.parent, os.W_OK):
        return CheckResult("queue", False, f"not writable: {path}", "fix its permissions")
    note = "" if path.exists() else " (will be created on first promote)"
    return CheckResult("queue", True, f"{path}{note}")


def _notify_check(config: Config) -> CheckResult:
    if not config.notify.desktop:
        return CheckResult("notify", True, "desktop notifications are off")
    if shutil.which("notify-send") is None:
        return CheckResult(
            "notify",
            False,
            "notify-send not found",
            "install libnotify (`sudo dnf install libnotify`) or set notify.desktop: false",
        )
    return CheckResult("notify", True, "notify-send is available")


def _scheduler_check(runner: Runner) -> CheckResult:
    fix = "run `jobpoller schedule install`"
    try:
        state = status(runner)
    except ScheduleError as exc:
        return CheckResult("scheduler", False, str(exc), fix)
    if not state.installed:
        return CheckResult("scheduler", False, "no jobpoller.timer installed", fix)
    if not (state.enabled and state.active):
        return CheckResult("scheduler", False, "timer is installed but not active", fix)
    return CheckResult("scheduler", True, f"next run: {state.next_run}")


def run_checks(
    config_path: Path,
    transport: httpx.BaseTransport | None = None,
    *,
    gpu: bool = True,
    runner: Runner | None = None,
) -> list[CheckResult]:
    runner = runner or run_systemctl  # looked up at call time so tests can patch it
    if not config_path.exists():
        return [
            CheckResult(
                "config",
                False,
                f"{config_path} does not exist",
                "run `jobpoller init` and answer y, or copy config.example.yaml",
            ),
            _scheduler_check(runner),
        ]
    try:
        config = load_config(config_path)
    except (OSError, ConfigError, ValueError) as exc:
        return [
            CheckResult("config", False, f"cannot load: {exc}", f"fix {config_path}"),
            _scheduler_check(runner),
        ]
    results = [CheckResult("config", True, str(config_path))]
    if config.llm.enabled:
        results += _llm_checks(config, transport, gpu)
    else:
        results.append(CheckResult("llm", True, "disabled (keyword-only runs)"))
    results += [_queue_check(config), _notify_check(config), _scheduler_check(runner)]
    return results
