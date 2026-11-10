from pathlib import Path

import pytest

from jobpoller import doctor
from jobpoller.cli import main
from jobpoller.config import PROFILES, ConfigError, load_config
from jobpoller.doctor import EXAMPLE_CONFIG, run_checks


def cfg(tmp_path: Path, llm_yaml: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(f"llm:\n  enabled: false\n{llm_yaml}")
    return path


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_each_profile_sets_its_preset(tmp_path: Path, name: str) -> None:
    llm = load_config(cfg(tmp_path, f"  profile: {name}\n")).llm
    preset = PROFILES[name]
    assert (llm.model, llm.num_ctx, llm.max_new_rankings, llm.timeout_seconds) == (
        preset["model"],
        preset["num_ctx"],
        preset["max_new_rankings"],
        preset["timeout_seconds"],
    )


def test_default_is_heavy_and_matches_old_defaults(tmp_path: Path) -> None:
    llm = load_config(cfg(tmp_path, "")).llm
    assert (llm.profile, llm.model, llm.num_ctx, llm.max_new_rankings) == (
        "heavy",
        "qwen3:14b",
        8192,
        40,
    )


def test_explicit_keys_override_the_profile(tmp_path: Path) -> None:
    llm = load_config(cfg(tmp_path, "  profile: light\n  model: qwen3:8b\n  num_ctx: 2048\n")).llm
    assert (llm.model, llm.num_ctx) == ("qwen3:8b", 2048)
    assert llm.max_new_rankings == PROFILES["light"]["max_new_rankings"]


def test_unknown_profile_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="light"):
        load_config(cfg(tmp_path, "  profile: turbo\n"))


def test_shipped_example_loads_for_every_profile(tmp_path: Path) -> None:
    for name in PROFILES:
        target = tmp_path / f"{name}.yaml"
        doctor.create_config_from_example(target, name)
        assert load_config(target).llm.profile == name


def test_gpu_check_is_skipped_for_cpu_profiles(tmp_path: Path) -> None:
    path = cfg(tmp_path, "  profile: light\n  resume: r.md\n").with_name("c.yaml")
    path.write_text("llm:\n  enabled: true\n  profile: light\n  resume: r.md\n")
    (tmp_path / "r.md").write_text("resume")
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/api/chat" not in request.url.path  # must not load the model just to measure
        return httpx.Response(200, json={"models": [{"name": "qwen3:4b"}]})

    results = {r.name: r for r in run_checks(path, httpx.MockTransport(handler), gpu=True)}
    assert results["model"].ok and results["gpu"].ok and "skipped" in results["gpu"].detail


def test_init_profile_flag_writes_profile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert EXAMPLE_CONFIG.is_file()
    target = tmp_path / "config.yaml"
    main(["init", "--yes", "--no-gpu", "--profile", "light", "--config", str(target)])
    assert load_config(target).llm.profile == "light"
    assert "ollama pull qwen3:4b" in capsys.readouterr().out
