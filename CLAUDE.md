# jobpoller: project rules for Claude Code

Personal job-search assistant. Polls public job-board JSON, filters/scores, optionally ranks
with a local Ollama model, and queues matches for a human to review and apply to by hand.

## Hard rules (never break these)

- Read-only against the outside world. Only documented, unauthenticated, public endpoints:
  Greenhouse Job Board API, Lever Postings API, Ashby posting API, HN Algolia, plus localhost
  Ollama. No logins, no cookies, no session reuse, no headless-browser scraping, no CAPTCHA
  work, no LinkedIn/Indeed/Glassdoor scraping, and **no auto-submitting applications**. If a
  task seems to need any of these, stop and ask.
- No secrets, resumes, or personal data in the repo. `config.yaml`, `resume.md`, `*.sqlite3`,
  `discovered.md`, `digest.html` stay gitignored. Only `config.example.yaml` is committed.
- Tests are offline: mock HTTP with `httpx.MockTransport`. Never hit the network in tests.
- The pipeline must stay idempotent: running twice adds nothing the second time.
- DB changes go through numbered migrations (`PRAGMA user_version`); never break an existing DB.

## Stack and standards

- Python >= 3.10, `httpx`, `PyYAML`/`ruamel.yaml` (comment-preserving writes), SQLite, `textual`
  for the TUI. Keep dependencies minimal and justify any new one.
- `ruff check .`, `mypy --strict jobpoller`, `pytest` must all pass before every commit.
- Small, typed, single-purpose modules. Pure functions where possible (scoring, parsing,
  rendering) so they test without I/O. Keep the existing architecture:
  `config -> sources -> scoring -> ranker -> store -> output`, orchestrated by `pipeline.py`.
- Small commits, one logical change each, imperative messages.

## Working with me

- I'm a strong Python/backend developer who wants to get better at systems concepts and Rust.
  Be direct; skip hedging and filler. When you make a non-obvious design call, give the reason
  in one or two sentences. Push back if my request has a flaw.
- Environment: Fedora Linux, Radeon RX 7900 GRE (16 GB), 32 GB RAM, Ollama with `qwen3:14b`.
- Don't claim something works unless you ran it. Say what you verified and what you couldn't.
