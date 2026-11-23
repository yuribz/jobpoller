# jobpoller

A personal job-search assistant pipeline. I built it for my own search and I'm sharing it as a
working example, not as a product: it finds new postings, filters and scores them, ranks the
survivors against my resume with a local LLM, and queues the matches for me to read and apply
to **by hand**.

```
public job-board JSON  ->  keyword filter/score  ->  local LLM fit ranking  ->  review queue
(Greenhouse, Lever,         (title, location,         (Ollama, scored vs        (SQLite + markdown;
 Ashby, HN)                  age, weighted words)      your resume)              triage via CLI/MCP/chat)
```

## Design constraints

These are deliberate and enforced by the code and tests:

- **Read-only against the outside world.** Only documented, unauthenticated, public endpoints
  (Greenhouse Job Board API, Lever Postings API, Ashby posting API, HN Algolia) plus localhost
  Ollama. No logins, cookies, headless browsers, LinkedIn/Indeed scraping or CAPTCHA work.
- **It never applies.** Nothing submits an application. A human reads the queue and applies.
- **Private by construction.** Resume, config, database and logs are gitignored; the resume is
  only ever sent to a model on localhost.
- **Idempotent.** Running the pipeline twice adds nothing the second time.
- **Safe schema changes.** SQLite changes go through numbered migrations (`PRAGMA user_version`).
- **Offline tests.** All HTTP is mocked with `httpx.MockTransport`; the suite never touches the
  network.

## What's in it

| Area | Modules |
|---|---|
| Fetch | `sources.py` (board and HN adapters), `manual.py` (add a posting you saved yourself) |
| Score | `scoring.py` (pure keyword scoring), `ranker.py` (Ollama fit ranking, JSON-schema output, cached) |
| Store | `store.py`, `migrations.py` (SQLite), `queuefile.py` (promote into a hand-edited markdown table) |
| Orchestrate | `pipeline.py`, `cli.py`, `config.py` |
| Triage | `tools.py` (one tool layer shared by) `mcp_server.py` (Claude Desktop / MCP) and `chat.py` (local Ollama chat) |
| Operate | `schedule.py` (systemd user timer), `notify.py`, `logsetup.py`, `doctor.py`, `bench.py`, `tune.py`, `digest.py` |

About 4.5k lines of typed Python, with `mypy --strict`, `ruff` and ~180 tests passing.
Scoring, parsing and rendering are pure functions so they test without I/O.

## How it was built

Most of the code was written with [Claude Code](https://claude.com/claude-code), working under
the rules in [CLAUDE.md](CLAUDE.md): the hard constraints above, the architecture to keep, and
gates (`ruff`, `mypy --strict`, `pytest`) that must pass before every commit. I set the design,
reviewed the changes, and kept the history in small single-purpose commits.

## Try it yourself

Linux is the tested platform (the scheduler uses systemd user timers and notifications use
`notify-send`; the rest is portable Python). You need Python 3.10+ and, for LLM ranking,
[Ollama](https://ollama.com). The LLM is optional: with `llm.enabled: false` it runs keyword-only.

```bash
git clone <this repo> && cd jobpoller
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"                  # installs dependencies; `.[mcp]` adds the MCP server
cp config.example.yaml config.yaml       # then edit boards, criteria and llm
python -m jobpoller check                # do the board slugs and the LLM respond?
python -m jobpoller run --dry-run        # preview, writes nothing
python -m jobpoller run                  # queue new matches into discovered.md
python -m jobpoller list                 # review them
```

`python -m jobpoller init` is a guided checklist that creates the config and tells you what's
still missing. The example config ships with sample company slugs and criteria that are mine:
**edit them** (`boards`, `criteria`, `llm.preferences`) before trusting the output.

For LLM ranking, pull a model and put your resume (`.md` or `.txt`) at `llm.resume`:

```bash
ollama pull qwen3:14b      # ~9 GB; `llm.profile: light` uses a 4B model if you have no big GPU
```

Run the checks:

```bash
ruff check . && mypy --strict jobpoller && pytest
```

The full walkthrough (config, the daily loop, scheduling, tuning, troubleshooting) is in
[GUIDE.md](GUIDE.md).

## LLM ranking notes

- Calls Ollama's native `/api/chat` with `think: false` (skips Qwen3's reasoning, much faster)
  and a JSON schema so output is always parseable.
- Each posting gets `fit` (0-100), a one-line summary and red flags (seniority, clearance,
  stack mismatch). Jobs below `min_fit` are dropped.
- Rankings are cached in SQLite keyed by job + model + resume + preferences, so reruns cost
  nothing unless you change one of those.
- If Ollama is down, remaining jobs are **deferred**, not queued unranked, so nothing is lost.

## Caveats

- Board slugs go stale when companies change ATS; `check` tells you which still respond.
- Greenhouse `updated_at` changes when a posting is edited, so age filtering is approximate.
- HN postings have no fixed format; the first `|`-separated field is treated as the company.
- This is a personal tool. Issues and PRs may go unanswered.

## License

[MIT](LICENSE)
