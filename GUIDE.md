# jobpoller: how to use it

jobpoller checks company job boards for you every day, throws away what doesn't fit, asks a
local AI model how well each remaining job matches your resume, and queues the good ones.
**You** read the list and apply by hand. It never logs in anywhere and never submits anything.

```
job boards (public JSON) -> keyword filter -> local AI scores fit vs your resume
        -> queue (you review) -> promote the good ones into your own application table
```

---

## 1. First-time setup (about 10 minutes)

```bash
git clone <this repo> && cd jobpoller
python -m venv .venv && source .venv/bin/activate
pip install -e .            # add ".[dev]" if you also want the test tools
```

You also need [Ollama](https://ollama.com) running with a model pulled (this project is tuned
for `qwen3:14b`, about 9 GB):

```bash
ollama pull qwen3:14b
```

Pick the model that fits your hardware with a **profile** (`llm.profile` in `config.yaml`):

| Profile | Model | For |
|---|---|---|
| `light` | `qwen3:4b` | No discrete GPU (integrated graphics), 16 GB RAM or less |
| `balanced` | `qwen3:8b` | No discrete GPU but patient, or a GPU with 8 GB |
| `heavy` (default) | `qwen3:14b` | Discrete GPU with 12+ GB VRAM |

Ollama normally runs `light` and `balanced` on the CPU, not on integrated AMD graphics, so
expect them to be much slower than `heavy` on a real GPU. They compensate by ranking fewer new
jobs per run (the rest wait for the next run) and allowing a longer timeout. Smaller models
score less reliably; `jobpoller bench` (section 6) compares them on your own jobs. Any `llm:`
key you set yourself (`model`, `num_ctx`, ...) overrides the profile.

```bash
jobpoller init --profile light     # creates config.yaml for a laptop without a GPU
ollama pull qwen3:4b
```

Then let the setup checklist tell you what's left:

```bash
jobpoller init
```

`init` offers to create `config.yaml` for you, then checks everything and prints a **fix**
under each problem. A fully set up machine looks like this:

```
ok    config: config.yaml
ok    resume: resume.md
ok    ollama: reachable at http://localhost:11434
ok    model: qwen3:14b is pulled
ok    gpu: qwen3:14b is 100% on GPU (10.5 GB)
ok    queue: /home/you/jobs/job_queue.md
ok    notify: notify-send is available
ok    scheduler: next run: Tue 2026-11-10 08:00:00 PST
```

Things you provide yourself:

| What | Where | Notes |
|---|---|---|
| Your resume | `resume.md` | Plain text or markdown (not .docx). It stays on your machine. |
| Your queue file | `queue_path:` in `config.yaml` | The markdown table you already keep, see section 4. |

`config.yaml`, `resume.md`, the database and `digest.html` are all git-ignored, so none of your
personal data can be committed by accident.

---

## 2. The config file in 2 minutes

Everything lives in `config.yaml` (the commented template is `config.example.yaml`). The parts
you will actually touch:

```yaml
boards:                          # companies to watch
  greenhouse: [cloudflare, gitlab]
  lever: [zoox]
  ashby: [ramp, supabase]
hn: true                         # also read the monthly "Who is hiring?" thread

criteria:
  include_title: [software, backend, platform]   # title must contain one of these
  exclude_title: [staff, principal, manager]     # ...and none of these
  locations: [san diego, remote]
  exclude_locations: [germany, india]
  min_score: 12                  # keyword score needed to count as a match
  keywords: {python: 8, rust: 6, php: -4}        # weight per keyword found in the posting

llm:
  enabled: true
  model: qwen3:14b
  resume: resume.md
  min_fit: 50                    # AI fit below this is not queued
  preferences: >-
    Backend and systems work. San Diego or remote. Entry-to-mid level.

queue_path: ~/jobs/job_queue.md  # where `promote` writes
schedule_time: "08:00"           # daily run time (keep the quotes)
notify: {threshold: 80}          # desktop notification when a new job scores >= 80
```

**Adding a company:** its board slug is the last part of its careers URL:
`boards.greenhouse.io/`**`cloudflare`**, `jobs.lever.co/`**`zoox`**, `jobs.ashbyhq.com/`**`ramp`**.
Add it under the matching ATS, then run `jobpoller check` to confirm it responds.

**How the AI score reads:** 80-100 strong match worth tailoring for, 60-79 good with gaps,
40-59 stretch, below 40 poor. It is told to penalize seniority and clearance mismatches.

---

## 3. The daily loop

```bash
jobpoller run                  # fetch, filter, score, queue new matches (1-2 minutes)
jobpoller list                 # what's queued, best first, with ids
jobpoller digest --open        # prettier: a web page with search and a min-fit slider
```

Then triage by id:

```bash
jobpoller promote 3 7          # add to your application queue and mark promoted
jobpoller dismiss 5 9          # not interested, never shown again
jobpoller applied 3            # you applied
```

To read one job in full (the whole posting, not just the summary):

```bash
jobpoller show 3               # readable: header, AI summary, flags, full posting text
jobpoller show 3 --json        # same data for other tools (see below)
```

The posting text is saved when a job is queued, so it stays readable after the company takes
the posting down. Jobs queued before this feature get their text filled in only if it was still
in the recent-jobs cache; otherwise `show` says to open the link.

### Adding a posting yourself (LinkedIn, an email, a friend's tip)

jobpoller never fetches LinkedIn or any other page for you. You save the posting, it reads the
file:

```bash
# 1. On the posting page: select the description, copy it, save as job.txt / job.md
#    (or "Save Page As" -> job.html)
jobpoller add job.txt --company "Acme" --title "Backend Engineer" \
    --url "https://www.linkedin.com/jobs/view/3812345678/"
pbpaste | jobpoller add --company "Acme" --title "Backend Engineer" --url "..."   # from stdin
jobpoller add job.html --url "..." --promote     # also puts it in your queue file
```

- Accepts `.txt`, `.md` and `.html` (a PDF or .docx: convert to text first, e.g. `pdftotext`).
- Saved HTML pages that contain the standard `JobPosting` data (LinkedIn, many company sites)
  fill in company, title, location and date by themselves; flags override. Scripts, menus
  and page chrome are dropped.
- The link is cleaned (tracking parameters removed, LinkedIn links reduced to
  `/jobs/view/<id>/`). Adding the same link again, or a link the poller already found, changes
  nothing.
- The AI scores it against your resume (`--no-score` to skip; if the AI is down it is added
  unscored). Your keyword filters never reject it, since you picked it.
- It then behaves like any other job: `list`, `show`, `promote`, `dismiss`, `applied`, and
  `show --json` for companion tools. Its source is `manual`.

Handy variations:

```bash
jobpoller list --status new           # only what you haven't triaged
jobpoller list --min-fit 75 -v        # strong matches, with link, summary and red flags
jobpoller list -m                      # Markdown: headers per status, bold fit, linked, red flags (pipe into a viewer like glow, or paste into notes)
jobpoller digest --since 3d           # only jobs first seen in the last 3 days
jobpoller run --dry-run               # preview without queuing anything
```

- Running `run` twice in a row adds nothing the second time. A job is shown once, ever.
- Anything you dismissed, promoted or applied to never comes back.
- On the digest page, **Copy queue row** puts a ready-made table row on your clipboard.

---

## 4. How `promote` treats your queue file

`promote` adds one row to a table with exactly these columns:

```
| # | Role | Company | Apply link | Resume | Cover letter | Status |
```

- Resume and Cover letter are left blank for you to fill in; Status is `discovered (fit 85)`.
- The `#` continues from the highest number already in your table.
- If there is no such table, it adds a `## Discovered by jobpoller` section at the end.
- It inserts a single line and touches nothing else, so rows you wrote by hand are safe.
- Promoting the same job twice, or a link that is already in the file, adds nothing.
- If the file can't be written, the status change is rolled back so nothing is lost.

---

## 5. Make it run by itself

```bash
jobpoller schedule install --dry-run    # shows exactly what it would do
jobpoller schedule install              # installs a systemd user timer (daily at schedule_time)
jobpoller schedule status               # next run, last result
jobpoller schedule remove
```

- Pick another time with `--time 07:30`, or set `schedule_time` in the config.
- If the computer was off at run time, the run happens next time it's on (`Persistent=true`).
- Logs: `journalctl --user -u jobpoller.service -n 50`.
- When a new job scores at or above `notify.threshold`, you get one desktop notification.
- If `schedule status` says lingering is off, runs only happen while you're logged in. That's
  fine for a desktop; for always-on, run `loginctl enable-linger $USER`.

---

## 6. Tuning what you see

Too much noise, or good jobs missing? Edit `criteria:` in `config.yaml`, then ask what the
edit would have done:

```bash
jobpoller tune
```

It re-scores the last 14 days of jobs jobpoller has fetched (no network, no AI, under a
second) and compares your edited settings with the ones your **last real `run`** used.
Example output (numbers illustrative):

```
Passing: 23 -> 41 of 3677 cached jobs (+18 newly pass, -0 newly fail)

Newly passing:
  +  14  Backend Engineer @ supabase (ashby)   was: score 8 < min 12
Newly failing:
  -  22  Senior Backend Engineer @ gitlab (greenhouse)   now: title:excluded 'senior'  [in your inbox]

Where jobs are rejected today:
   2796  title:no include match
    315  title:excluded 'senior'
```

- **Newly passing** are jobs your edit would let through, and *why they failed before*.
- **Newly failing** are jobs your edit would now drop. `[in your inbox]` means you've already
  been shown that one, so the edit won't remove it from `list`.
- **Where jobs are rejected today** tells you which setting matters most. If almost
  everything dies on `title:no include match`, widen `include_title`; if one excluded word
  kills hundreds, check it isn't too broad.
- **Top 15** are the best matches under the new settings.

When you're happy, `jobpoller run` applies the settings to new jobs. (Jobs that newly pass
get queued on the next run only if they're still posted on the board.)

Other options: `--against old.yaml` compares with a copy of an older config (handy: `cp
config.yaml config.old.yaml` before a big edit), and `--limit 30` shows more rows.
`tune` covers the filters and keyword weights only; the AI fit score isn't recomputed.

What to change for what problem:

| Problem | Change |
|---|---|
| Wrong kinds of roles | `include_title` / `exclude_title` |
| Wrong places | `locations`, `exclude_locations` |
| Not enough matches | lower `min_score` or add keywords |
| Too many weak matches | raise `min_score` or `llm.min_fit` |
| AI scores feel off | rewrite `llm.preferences` (be specific about seniority and stack) |
| Posting contains a deal-breaker | add the phrase to `exclude_text` (e.g. `ts/sci`) |

Good to know:
- Jobs already in your inbox stay there; new settings affect jobs found from now on.
- Changing your resume, `preferences` or model **re-ranks everything** (the first run after
  is slower).
- A job the AI scored below `min_fit` is simply not queued. Its score is cached, so checking
  it again on later runs costs nothing.

### Picking an AI model with evidence

```bash
ollama pull qwen3:8b
jobpoller bench --models qwen3:8b,qwen3:14b -n 15
```

It scores the same jobs with each model and prints speed, how closely they agree, and the
jobs they disagree on most. On the reference machine (RX 7900 GRE) `qwen3:14b` takes about
3 seconds per job once loaded and the first call after idle about 10 seconds.

---

## 7. Command reference

| Command | What it does |
|---|---|
| `init [--yes] [--no-gpu]` | Guided setup checklist; offers to create `config.yaml` |
| `check` | Does every board slug (and the AI model) respond? |
| `run [--dry-run]` | Fetch, filter, score, queue new matches |
| `list [--status S] [--min-fit N] [-v]` | Show queued jobs, best first |
| `show ID [--json]` | One job with its full posting text |
| `add [FILE] --company --title --url [--promote] [--no-score]` | Add a posting you saved (txt, md, html) |
| `promote ID...` | Add to your queue file, mark promoted |
| `dismiss ID...` | Hide for good |
| `applied ID...` | Mark applied |
| `digest [--open] [--since 7d] [--min-fit N] [--out FILE]` | Write the HTML page |
| `schedule install\|remove\|status [--time HH:MM] [--dry-run]` | Daily systemd timer |
| `tune [--against FILE] [--limit N]` | Preview what a criteria edit would change |
| `bench --models A,B [-n 20]` | Compare AI models |

Every command accepts `--config path/to/config.yaml` (default `./config.yaml`).
With the venv active it's `jobpoller ...`; otherwise `python -m jobpoller ...`.

---

## 8. When something's wrong

Start with `jobpoller init`; it names the problem and the fix for most of these.

| Symptom | Fix |
|---|---|
| `model 'qwen3:14b' not pulled` | `ollama pull qwen3:14b` |
| `cannot reach Ollama` | `systemctl start ollama` (or run `ollama serve`) |
| `gpu: ... running on the CPU` | Ollama is missing the ROCm build, or can't open `/dev/kfd` and `/dev/dri` (needs the `render` and `video` groups). Look for GPU lines in `journalctl -u ollama -b`. |
| `gpu: only 60% ... fits in VRAM` | Lower `llm.num_ctx`, or use a smaller model. Partial offload is slow. |
| `warn: ... 404` for a board | The company moved or renamed its board. Find the new slug or remove it. |
| `warn: ... timed out` | One board was slow; the run continued without it. Try again later. |
| Many boards failing at once | Likely rate-limiting or no network. Raise `request_delay_seconds` and rerun; nothing is lost. |
| `deferred N` in the run summary | The AI was down or hit `max_new_rankings`. Those jobs are retried next run. |
| `promote needs queue_path` | Add `queue_path:` to `config.yaml`. |
| No desktop notification | `jobpoller init` checks `notify-send`. Install `libnotify` or set `notify.desktop: false`. Notifications only fire for jobs with an AI score at or above the threshold, so keyword-only runs never notify. |
| Timer didn't fire | `jobpoller schedule status`, then the journal command in section 5. |
| Odd HN job titles | Hacker News posts have no fixed format, so titles and company names can come out messy. Open the link; the digest shows the AI's summary. |

---

## 9. What leaves your machine

- Requests to the public job-board endpoints (Greenhouse, Lever, Ashby) and Hacker News's
  Algolia API. No logins, cookies, or scraping of LinkedIn/Indeed.
- Calls to Ollama on `localhost`. Your resume and the AI scores never go anywhere else.
- Only if you set `notify.ntfy`: the top job's title is sent to that notification server.
  It's off by default.

---

## 10. For companion tools: `show --json`

Other programs (for example a resume tailor) should read jobs through
`jobpoller show ID --json`, not the SQLite file; the schema is internal and may change. The
output is one JSON object with `schema_version: 1` and these keys: `id`, `status`, `company`,
`title`, `url`, `source` (`greenhouse`, `lever`, `ashby`, `hn` or `manual`), `location`, `posted_at` (ISO or null), `first_seen`, `fit` (integer or
null), `keyword_score`, `summary`, `red_flags` (list), `description` (plain text, may be empty).
A breaking change bumps `schema_version`. An unknown id exits 1 with the error on stderr.
