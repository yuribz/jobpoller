# jobpoller

Finds new jobs; you do the applying. Polls public job-board JSON (Greenhouse, Lever, Ashby)
plus the newest HN "Who is hiring?" thread, filters and scores against your keywords,
optionally ranks the survivors against your resume with a **local Ollama model**, and appends
only *new* matches to `discovered.md`.

No logins, no cookies, no LinkedIn/Indeed scraping, no auto-submit. Nothing leaves your machine
except requests to the public job-board endpoints.

## Use

**New here? Read [GUIDE.md](GUIDE.md)**: setup, the daily loop, scheduling, and troubleshooting.

```bash
pip install -e ".[dev]"
cp config.example.yaml config.yaml     # edit boards, criteria, llm
python -m jobpoller check              # do the slugs and the LLM respond?
python -m jobpoller run --dry-run      # preview
python -m jobpoller run                # append new matches to discovered.md
```

Schedule it daily, e.g. cron: `0 8 * * * cd ~/jobpoller && python -m jobpoller run`.

## Local LLM ranking (Ollama)

```bash
ollama pull qwen3:14b                  # ~9 GB; fits a 16 GB GPU with room for context
ollama ps                              # after a first run: should say 100% GPU
```

Set `llm.enabled: true` and point `llm.resume` at your resume as `.md`/`.txt`.

- Calls Ollama's native `/api/chat` with `think: false` (skips Qwen3's reasoning, much faster)
  and a JSON schema so output is always parseable.
- Each posting gets `fit` (0-100), a one-line summary and red flags (seniority, clearance,
  stack mismatch). Jobs below `min_fit` are dropped.
- Rankings are cached in SQLite keyed by job + model + resume + preferences. Edit your resume
  or switch models and affected jobs are re-ranked; otherwise reruns cost nothing.
- Best keyword matches are ranked first, up to `max_new_rankings` per run; the rest wait for
  the next run.
- If Ollama is down or the model isn't pulled, remaining jobs are **deferred**, not queued
  unranked, so nothing is lost. Set `llm.enabled: false` for keyword-only runs.

## Notes

- Dedupe lives in `jobpoller.sqlite3`; only queued jobs are recorded as seen.
- A failing board (bad slug, outage) is a warning; the rest still run.
- Greenhouse `updated_at` can change when a posting is edited, so age filtering is approximate.
- HN postings have no fixed format; the first `|`-separated field is treated as the company.
- Tests run offline against mocked responses: `pytest`, `mypy jobpoller`, `ruff check .`.
