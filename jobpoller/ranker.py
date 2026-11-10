"""Optional LLM fit ranking against a local Ollama server.

Uses Ollama's native /api/chat (not the OpenAI-compatible route) because it supports
`think: false` for Qwen3 and JSON-schema constrained output via `format`.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import httpx

from .config import LLMConfig
from .models import Job, Ranking

PROMPT_VERSION = "1"

SYSTEM = (
    "You screen job postings for ONE candidate, using only the resume and preferences provided. "
    "Score how well the candidate fits the posting from 0 to 100: "
    "80-100 strong match worth tailoring an application for; 60-79 good match with minor gaps; "
    "40-59 stretch; 0-39 poor match. "
    "Penalize heavily: required years of experience well above the candidate's, security "
    "clearance or citizenship requirements not evidenced in the resume, and core stack mismatch. "
    "Never credit skills the resume does not show. "
    "summary: one plain sentence (max 200 characters) saying why. "
    "red_flags: up to 5 short phrases, empty if none. Respond with JSON only."
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "fit": {"type": "integer", "minimum": 0, "maximum": 100},
        "summary": {"type": "string"},
        "red_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["fit", "summary", "red_flags"],
}

_THINK = re.compile(r"<think>.*?</think>", re.S)


class LLMUnavailable(Exception):
    """The server is unreachable or the model is missing; no job can be ranked right now."""


class RankError(Exception):
    """One job could not be ranked (timeout, malformed output); others may still work."""


def parse_ranking(content: str) -> Ranking:
    text = _THINK.sub("", content).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise RankError("no JSON object in model output")
    try:
        data = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise RankError(f"invalid JSON from model: {exc}") from exc
    fit = data.get("fit") if isinstance(data, dict) else None
    if isinstance(fit, bool) or not isinstance(fit, (int, float)):
        raise RankError("model output missing numeric 'fit'")
    flags_raw = data.get("red_flags")
    flags = tuple(str(f)[:100] for f in flags_raw[:5]) if isinstance(flags_raw, list) else ()
    return Ranking(
        fit=max(0, min(100, int(fit))),
        summary=str(data.get("summary", ""))[:300],
        red_flags=flags,
    )


class Ranker:
    def __init__(self, cfg: LLMConfig, resume: str, client: httpx.Client) -> None:
        self._cfg = cfg
        self._resume = resume.strip()
        self._client = client
        material = "\0".join(
            [PROMPT_VERSION, cfg.model, self._resume, cfg.preferences, str(cfg.max_desc_chars)]
        )
        # Cache key part: changing the model, resume, preferences or prompt re-ranks everything.
        self.fingerprint = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def close(self) -> None:
        self._client.close()

    def check(self) -> str:
        try:
            response = self._client.get(f"{self._cfg.base_url}/api/tags")
            response.raise_for_status()
            names = {m.get("name") for m in response.json().get("models", [])}
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise LLMUnavailable(f"cannot reach Ollama at {self._cfg.base_url}: {exc}") from exc
        model = self._cfg.model
        if model not in names and f"{model}:latest" not in names:
            raise LLMUnavailable(f"model '{model}' not pulled (run: ollama pull {model})")
        return f"{model} ready at {self._cfg.base_url}"

    def _messages(self, job: Job) -> list[dict[str, str]]:
        description = job.description[: self._cfg.max_desc_chars]
        user = (
            f"CANDIDATE RESUME\n{self._resume}\n\n"
            f"CANDIDATE PREFERENCES\n{self._cfg.preferences or '(none given)'}\n\n"
            "JOB POSTING\n"
            f"Title: {job.title}\nCompany: {job.company}\n"
            f"Location: {job.location}{' (remote)' if job.remote else ''}\n"
            f"Description:\n{description}"
        )
        return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]

    def rank(self, job: Job) -> Ranking:
        payload = {
            "model": self._cfg.model,
            "messages": self._messages(job),
            "stream": False,
            "think": False,
            "format": SCHEMA,
            "keep_alive": self._cfg.keep_alive,
            "options": {"temperature": 0, "num_ctx": self._cfg.num_ctx},
        }
        try:
            response = self._client.post(f"{self._cfg.base_url}/api/chat", json=payload)
        except httpx.ConnectError as exc:
            raise LLMUnavailable(f"cannot reach Ollama at {self._cfg.base_url}") from exc
        except httpx.HTTPError as exc:
            raise RankError(f"request failed: {exc!r}") from exc
        if response.status_code == 404:
            raise LLMUnavailable(
                f"model '{self._cfg.model}' not found (run: ollama pull {self._cfg.model})"
            )
        try:
            response.raise_for_status()
            content = response.json()["message"]["content"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise RankError(f"unexpected response: {exc!r}") from exc
        return parse_ranking(str(content))
