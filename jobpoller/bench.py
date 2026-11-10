"""Compare Ollama models on the same jobs: latency, agreement, biggest disagreements.

`compare` is pure (no I/O) so the statistics are testable offline; `run_bench` does the calls.
"""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

import httpx

from .config import LLMConfig
from .models import Job
from .ranker import LLMUnavailable, Ranker, RankError


@dataclass
class ModelRun:
    model: str
    fits: dict[str, int | None] = field(default_factory=dict)  # job.key -> fit, None if failed
    latencies: list[float] = field(default_factory=list)  # seconds per successful call, in order
    failures: int = 0


@dataclass(frozen=True)
class ModelStats:
    model: str
    ok: int
    failures: int
    cold_s: float | None  # first call: includes loading the model into VRAM
    median_s: float | None  # remaining (warm) calls
    p95_s: float | None
    mean_fit: float | None


@dataclass(frozen=True)
class PairStats:
    a: str
    b: str
    n: int  # jobs both models ranked
    mean_abs_diff: float | None
    pearson: float | None  # None if undefined (fewer than 2 points or constant scores)
    within_10: float | None  # share of jobs where the fits differ by <= 10


@dataclass(frozen=True)
class Disagreement:
    key: str
    title: str
    fits: dict[str, int]
    spread: int


@dataclass(frozen=True)
class Comparison:
    stats: list[ModelStats]
    pairs: list[PairStats]
    disagreements: list[Disagreement]


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]  # nearest-rank


def _stats(run: ModelRun) -> ModelStats:
    ok_fits = [f for f in run.fits.values() if f is not None]
    warm = run.latencies[1:]
    return ModelStats(
        model=run.model,
        ok=len(ok_fits),
        failures=run.failures,
        cold_s=run.latencies[0] if run.latencies else None,
        median_s=statistics.median(warm) if warm else None,
        p95_s=_percentile(warm, 0.95),
        mean_fit=statistics.fmean(ok_fits) if ok_fits else None,
    )


def _pair(a: ModelRun, b: ModelRun) -> PairStats:
    common = [
        (fa, fb)
        for key, fa in a.fits.items()
        if fa is not None and (fb := b.fits.get(key)) is not None
    ]
    if not common:
        return PairStats(a.model, b.model, 0, None, None, None)
    xs, ys = [float(p[0]) for p in common], [float(p[1]) for p in common]
    try:
        pearson: float | None = statistics.correlation(xs, ys)
    except statistics.StatisticsError:  # <2 points or a constant series
        pearson = None
    return PairStats(
        a.model,
        b.model,
        len(common),
        statistics.fmean(abs(x - y) for x, y in common),
        pearson,
        sum(1 for x, y in common if abs(x - y) <= 10) / len(common),
    )


def compare(runs: Sequence[ModelRun], titles: dict[str, str], top: int = 5) -> Comparison:
    pairs = [_pair(runs[i], runs[j]) for i in range(len(runs)) for j in range(i + 1, len(runs))]
    keys = [k for k in runs[0].fits] if runs else []
    rows: list[Disagreement] = []
    for key in keys:
        fits = {r.model: f for r in runs if (f := r.fits.get(key)) is not None}
        if len(fits) >= 2:
            spread = max(fits.values()) - min(fits.values())
            rows.append(Disagreement(key, titles.get(key, key), fits, spread))
    rows.sort(key=lambda d: -d.spread)
    return Comparison([_stats(r) for r in runs], pairs, rows[:top])


def unload(client: httpx.Client, base_url: str, model: str) -> None:
    """Best-effort: evict the model from VRAM so the next model's cold start is measured fairly."""
    try:
        client.post(f"{base_url}/api/generate", json={"model": model, "keep_alive": 0})
    except httpx.HTTPError:
        pass


def run_bench(
    cfg: LLMConfig,
    resume: str,
    jobs: Sequence[Job],
    models: Sequence[str],
    client_factory: Callable[[], httpx.Client],
    log: Callable[[str], None] = lambda _: None,
) -> list[ModelRun]:
    """Rank the same jobs with each model in turn. Raises LLMUnavailable if a model is missing."""
    runs: list[ModelRun] = []
    for model in models:
        client = client_factory()
        ranker = Ranker(replace(cfg, model=model), resume, client)
        run = ModelRun(model)
        try:
            ranker.check()  # fail fast with the exact `ollama pull` hint
            for i, job in enumerate(jobs, 1):
                started = time.perf_counter()
                try:
                    fit: int | None = ranker.rank(job).fit
                except RankError as exc:
                    fit = None
                    run.failures += 1
                    log(f"  {model} {i}/{len(jobs)}: failed ({exc})")
                else:
                    run.latencies.append(time.perf_counter() - started)
                    log(f"  {model} {i}/{len(jobs)}: fit {fit} in {run.latencies[-1]:.1f}s")
                run.fits[job.key] = fit
        except LLMUnavailable:
            unload(client, cfg.base_url, model)
            client.close()
            raise
        unload(client, cfg.base_url, model)
        client.close()
        runs.append(run)
    return runs


def format_report(result: Comparison) -> str:
    def secs(v: float | None) -> str:
        return "   -  " if v is None else f"{v:6.1f}"

    lines = ["model                 ok fail  cold(s) median(s) p95(s) mean-fit"]
    for s in result.stats:
        mean = "-" if s.mean_fit is None else f"{s.mean_fit:.1f}"
        lines.append(
            f"{s.model:<20} {s.ok:3d} {s.failures:4d}  {secs(s.cold_s)}  {secs(s.median_s)}"
            f" {secs(s.p95_s)}  {mean:>8}"
        )
    for p in result.pairs:
        corr = "n/a" if p.pearson is None else f"{p.pearson:.2f}"
        mad = "n/a" if p.mean_abs_diff is None else f"{p.mean_abs_diff:.1f}"
        w10 = "n/a" if p.within_10 is None else f"{p.within_10:.0%}"
        lines.append(
            f"\n{p.a} vs {p.b} (n={p.n}): mean |diff| {mad}, pearson {corr}, within 10: {w10}"
        )
    if result.disagreements:
        lines.append("\nBiggest disagreements:")
        for d in result.disagreements:
            fits = ", ".join(f"{m} {f}" for m, f in d.fits.items())
            lines.append(f"  spread {d.spread:3d}  {d.title[:60]}  [{fits}]")
    return "\n".join(lines)
