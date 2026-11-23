from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .notify import NotifyConfig
from .schedule import ScheduleError, normalize_time
from .scoring import Criteria
from .sources import FETCHERS

DEFAULT_UA = "jobpoller/0.1 (personal job search; reads public job-board JSON only)"


# Presets for `llm.profile`. Any llm.* key you set yourself overrides the preset.
# light/balanced target machines without a discrete GPU (CPU inference): smaller model, smaller
# prompt window, fewer new rankings per run (the rest wait for the next run), longer timeout.
PROFILES: dict[str, dict[str, Any]] = {
    "light": {
        "model": "qwen3:4b",
        "num_ctx": 4096,
        "max_desc_chars": 3500,
        "max_new_rankings": 20,
        "timeout_seconds": 300,
    },
    "balanced": {
        "model": "qwen3:8b",
        "num_ctx": 6144,
        "max_desc_chars": 5000,
        "max_new_rankings": 30,
        "timeout_seconds": 300,
    },
    "heavy": {
        "model": "qwen3:14b",
        "num_ctx": 8192,
        "max_desc_chars": 6000,
        "max_new_rankings": 40,
        "timeout_seconds": 180,
    },
}
DEFAULT_PROFILE = "heavy"


@dataclass
class LLMConfig:
    profile: str = DEFAULT_PROFILE
    enabled: bool = False
    base_url: str = "http://localhost:11434"
    model: str = "qwen3:14b"
    resume_path: Path | None = None  # plain text / markdown
    preferences: str = ""
    min_fit: int = 50  # jobs ranked below this are not queued
    max_new_rankings: int = 40  # cap on uncached LLM calls per run
    max_desc_chars: int = 6000
    num_ctx: int = 8192
    timeout_seconds: float = 180.0  # first call also loads the model
    keep_alive: str = "10m"


@dataclass
class Config:
    boards: dict[str, list[str]] = field(default_factory=dict)
    hn: bool = True
    criteria: Criteria = field(default_factory=Criteria)
    llm: LLMConfig = field(default_factory=LLMConfig)
    db: Path = Path("jobpoller.sqlite3")
    output: Path = Path("discovered.md")
    delay_seconds: float = 0.5
    cache_days: int = 14  # how long fetched jobs are kept for offline re-scoring
    queue_path: Path | None = None  # your hand-edited application queue (markdown table)
    digest_path: Path = Path("digest.html")
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    schedule_time: str = "08:00"  # daily run time for `jobpoller schedule install`
    user_agent: str = DEFAULT_UA
    log_path: Path = Path("jobpoller.log")
    log_days: int = 30  # rotated logs older than this are deleted


class ConfigError(ValueError):
    pass


def _str_list(raw: Any, name: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ConfigError(f"{name} must be a list of strings")
    return raw


def _resolve(base: Path, value: object) -> Path:
    """Relative paths resolve against the config's folder; `~` expands to your home."""
    return base / Path(str(value)).expanduser()


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    base = path.parent

    boards_raw = raw.get("boards") or {}
    unknown = set(boards_raw) - set(FETCHERS)
    if unknown:
        raise ConfigError(
            f"unknown ATS in boards: {sorted(unknown)}; supported: {sorted(FETCHERS)}"
        )
    boards = {ats: _str_list(slugs, f"boards.{ats}") for ats, slugs in boards_raw.items()}

    c = raw.get("criteria") or {}
    keywords = c.get("keywords") or {}
    if not isinstance(keywords, dict) or not all(isinstance(v, int) for v in keywords.values()):
        raise ConfigError("criteria.keywords must map term -> integer weight")
    criteria = Criteria(
        include_title=_str_list(c.get("include_title"), "criteria.include_title"),
        exclude_title=_str_list(c.get("exclude_title"), "criteria.exclude_title"),
        locations=_str_list(c.get("locations"), "criteria.locations"),
        accept_remote=bool(c.get("accept_remote", True)),
        exclude_locations=_str_list(c.get("exclude_locations"), "criteria.exclude_locations"),
        exclude_text=_str_list(c.get("exclude_text"), "criteria.exclude_text"),
        keywords={str(k): int(v) for k, v in keywords.items()},
        min_score=int(c.get("min_score", 10)),
        max_age_days=c.get("max_age_days", 45),
    )

    llm_raw = raw.get("llm") or {}
    profile = str(llm_raw.get("profile", DEFAULT_PROFILE))
    if profile not in PROFILES:
        raise ConfigError(f"llm.profile must be one of {sorted(PROFILES)}, got {profile!r}")
    preset = PROFILES[profile]
    llm = LLMConfig(
        profile=profile,
        enabled=bool(llm_raw.get("enabled", False)),
        base_url=str(llm_raw.get("base_url", "http://localhost:11434")).rstrip("/"),
        model=str(llm_raw.get("model", preset["model"])),
        resume_path=_resolve(base, llm_raw["resume"]) if llm_raw.get("resume") else None,
        preferences=str(llm_raw.get("preferences", "")),
        min_fit=int(llm_raw.get("min_fit", 50)),
        max_new_rankings=int(llm_raw.get("max_new_rankings", preset["max_new_rankings"])),
        max_desc_chars=int(llm_raw.get("max_desc_chars", preset["max_desc_chars"])),
        num_ctx=int(llm_raw.get("num_ctx", preset["num_ctx"])),
        timeout_seconds=float(llm_raw.get("timeout_seconds", preset["timeout_seconds"])),
        keep_alive=str(llm_raw.get("keep_alive", "10m")),
    )
    if llm.enabled and llm.resume_path is None:
        raise ConfigError("llm.enabled requires llm.resume (path to your resume as .md/.txt)")

    cache_days = int(raw.get("cache_days", 14))
    if cache_days < 1:
        raise ConfigError("cache_days must be at least 1")

    n = raw.get("notify") or {}
    ntfy = str(n.get("ntfy") or "")
    if ntfy and not ntfy.startswith(("http://", "https://")):
        raise ConfigError("notify.ntfy must be a full http(s) topic URL")
    notify = NotifyConfig(
        threshold=int(n.get("threshold", 80)),
        desktop=bool(n.get("desktop", True)),
        ntfy=ntfy,
    )

    try:
        schedule_time = normalize_time(raw.get("schedule_time", "08:00"))
    except ScheduleError as exc:
        raise ConfigError(f"schedule_time: {exc}") from exc

    log_days = int(raw.get("log_days", 30))
    if log_days < 1:
        raise ConfigError("log_days must be at least 1")

    return Config(
        boards=boards,
        hn=bool(raw.get("hn", True)),
        criteria=criteria,
        llm=llm,
        db=_resolve(base, raw.get("db", "jobpoller.sqlite3")),
        output=_resolve(base, raw.get("output", "discovered.md")),
        delay_seconds=float(raw.get("request_delay_seconds", 0.5)),
        cache_days=cache_days,
        queue_path=_resolve(base, raw["queue_path"]) if raw.get("queue_path") else None,
        digest_path=_resolve(base, raw.get("digest", "digest.html")),
        notify=notify,
        schedule_time=schedule_time,
        user_agent=str(raw.get("user_agent", DEFAULT_UA)),
        log_path=_resolve(base, raw.get("log", "jobpoller.log")),
        log_days=log_days,
    )
