# LocalLLMAgent

A fully local agent system that runs on this Mac Mini, powered by a **local LLM
served by Ollama** — no Anthropic/Claude API calls at runtime, and no Claude
Code/Cowork-managed scheduling. The agent has a name: **Wren**. It works two
ways:

- **Scheduled tasks** (Strava-to-calendar logging, a morning brief email, daily
  activity + YouTube + AI-chat learnings entries) — unattended, triggered entirely
  by macOS `launchd`, no human in the loop.
- **Ad hoc chat** — a small always-on web app (`chat/server.py`) so the user can
  talk to Wren directly and have her take action on request, from anywhere,
  over Tailscale. See "Wren — ad hoc chat" below.

**Make it yours.** Personal preferences — whose agent this is, calendar
categories and colors, which job titles the opportunity scout flags, default
location — live in `config/preferences.json`, not in Python. Copy
`config/preferences.example.json` to `config/preferences.json`, edit it, do the
same for `agent/identity.example.md` → `agent/identity.md`, and Wren serves you
instead. Both real files are **gitignored** — they hold your name and where you
live, so they stay out of the repo; the `.example` versions are the committed
templates, and the code falls back to them so a fresh clone still runs. Every
key is documented in [docs/preferences.md](docs/preferences.md). Secrets stay
in the gitignored `config/.env`.

## Architecture

```
launchd (per-task .plist, timed)
   -> python -m tasks.<task_name>
       -> fetches data via tool modules (weather, calendar, Strava, Chrome history)
       -> calls the local model via Ollama's HTTP API for anything that
          needs natural-language composition or tool-calling
       -> writes the result out (email / calendar event / Markdown file)
       -> logs everything to logs/<task_name>.log
```

**The model is swappable.** Nothing in this codebase is Gemma-specific — the
model name is just `OLLAMA_MODEL` in `config/.env`. Swap models with
`ollama pull <model>` + edit that one line. The one thing to verify after a
swap: the **chat server** relies on Ollama's tool-calling protocol (`tools` /
`tool_calls`), so a model with weak tool-calling support may not drive it
reliably — the scheduled tasks only need plain text completion (or, for
`strava_download`, no model at all).

**Wren shares that Ollama with other projects, and it serves one request at a
time.** A long background job therefore starves chat silently — the queued
request just gets no bytes, which looks exactly like a dead server. Wren probes
`/api/ps` on a timeout and says which it actually was, and interactive turns
give up sooner than scheduled tasks do (`WREN_CHAT_MODEL_TIMEOUT`, 120s). The
separate, nastier case is the MLX runner wedging — healthy HTTP, no generation,
clears only on a runner kill. How to tell the two apart, and what has already
been ruled out as a cause, is in
[docs/ollama-serving.md](docs/ollama-serving.md).

**The backend is swappable too.** All model calls route through one seam
(`agent/loop.py:_llm_chat`), which defaults to local Ollama but can be pointed at
a cloud model (Gemini) via `WREN_LLM_BACKEND` — globally, or per-task with
`WREN_<TASK>_BACKEND` so chat stays local while a heavy scheduled task uses the
cloud. This sends that task's data off-device, the opposite of the local-first
default, so it's opt-in. See [docs/llm-backend.md](docs/llm-backend.md).

**Chat can escalate a weak answer to a frontier model, by hand.** When the user
judges a local reply too weak, a "Redo with the frontier model" button re-runs
that turn on a configured cloud backend (`WREN_ESCALATION_BACKEND`, provider-
neutral) — one deliberate, badged, logged tap. There's no automatic router: the user
is the router, and every escalation is recorded to `config/escalations.json` as
the paired dataset that would justify one later. See
[docs/frontier-escalation.md](docs/frontier-escalation.md).

Every chat call sets the context window explicitly via `OLLAMA_NUM_CTX`
(default 8192) rather than leaving it to Ollama's small default, which would
silently truncate the front of the prompt (where the system prompt lives). Each
call also logs the effective `num_ctx` and the actual prompt token count
(`prompt_tokens`) so you can watch how close a session runs to the ceiling. To
keep a single large tool result (e.g. a web search dumping page after page of
listings) from blowing past `num_ctx` — which triggers exactly that front-
truncation and, with the system prompt gone, a runaway repetition loop — each
tool result is capped at `OLLAMA_MAX_TOOL_RESULT_CHARS` (default 8000 chars,
~2000 tokens) before it's appended to the conversation. The conversation
history itself is budgeted too: before each chat turn the server drops the
oldest whole user-turns once the history exceeds `WREN_CHAT_MAX_HISTORY_CHARS`
(default 16000 chars, ~4k tokens — half the default window, leaving the rest
for the tool schemas and the turn's own growth), keeping the system prompt and
the newest turn. So a long session degrades gracefully — the model forgets the
oldest exchanges — instead of hitting `num_ctx` and losing its system prompt.

Chat also trims the *tool* overhead: instead of sending every registered tool
schema each turn, a session sends a small always-loaded core plus whichever tool
groups the turn needs, pulled in by keyword pre-load or the model's `load_tools`
meta-tool. Background tasks keep the full toolset. See
[docs/tool-loading.md](docs/tool-loading.md).

### `agent/loop.py` — how tasks and chat talk to the model

- **`advance(messages, tools, dispatch, confirm_before=...)`** /
  **`resolve(messages, call, approved, dispatch)`** — the resumable
  tool-calling loop both drivers (`chat/server.py` and `tasks/bg_worker.py`)
  are built on. Sends messages + tool schemas to Ollama's `/api/chat`,
  dispatches any `tool_calls` to local Python functions, feeds results back,
  and repeats (capped at `MAX_TOOL_ITERATIONS`) until the model returns a
  final text answer. `advance()` auto-executes any tool call whose name isn't
  in `confirm_before`; the moment one *is* in that set, it stops and returns
  the pending call without executing it, so the caller can show a human
  what's about to happen and come back later (`resolve()` then `advance()`
  again) to actually run it. A future model-driven task would call these
  directly, exactly as the worker does. (An earlier `run_agent()` convenience
  wrapper — `advance()` with nothing gated — was removed once nothing called
  it.)
- **`complete_text(...)`** — a single-turn, tool-free completion. Used when
  the surrounding structure (HTML layout, doc template) is built
  deterministically in Python and the model is only asked to write a
  paragraph of prose to slot in. Used by `morning_brief.py` and the daily
  learnings tasks — this is more reliable than trusting a small local
  model to produce well-formed HTML/markdown on its own.

The model call (`_ollama_chat`) **streams** from Ollama and reassembles the
reply, which gives `advance()` an interruption point: it takes an optional
`should_cancel` callback and, between chunks, raises `TurnCancelled` if it
fires. The chat server uses this for a **Stop button** — while a turn is
running the Send button becomes "Stop", which hits `POST /chat/cancel`; that
sets a per-session `threading.Event` the running turn is watching, so the
in-flight turn unwinds promptly (returning `{"type": "cancelled"}`) instead of
having to wait out a runaway generation or the read timeout. The partial turn
is rolled back so history stays clean. (Scheduled/background runs pass no
`should_cancel`, so they're unaffected.)

Every system prompt gets two persona layers prepended automatically (via
`with_identity()`), in order:
1. **`agent/wren.md`** — Wren's own identity: name, voice, personality traits.
   Present in *every* call, scheduled or chat, so she's a consistent agent
   either way.
2. **`agent/identity.md`** — who the user is and how they want things written
   (direct, concise, no flattery). This one is personal data, so it's
   **gitignored**; copy `agent/identity.example.md` and rewrite it as yourself.
   A checkout without it just runs on `wren.md` alone.

Both are hand-maintained and deliberately short — every token is paid for on
every call, scheduled or chat. A third file, **`agent/wren_chat.md`**, holds
*behavioral* instructions (ask questions when useful; call a gated tool in the
same turn rather than promising to — see
[docs/model-constraints.md](docs/model-constraints.md)) that only make sense in
an interactive session — it's loaded only by
`chat/server.py`, not injected into scheduled tasks, since those are
explicitly told not to ask questions or wait for confirmation (there's nobody
to ask at 5am). Since every task and the chat server talk to Ollama through
these functions, this is the only place persona/identity needs to be wired
in — nothing new inherits it automatically.

### `agent/tools/` — one module per capability

| Module | Purpose |
|---|---|
| `weather.py` | OpenWeatherMap forecast for `DEFAULT_LOCATION` |
| `web_search.py` | Web search via Tavily API |
| `web_fetch.py` | Fetch one web page as clean markdown via the Firecrawl API (`fetch_webpage`) — search finds pages, this reads one. Content is untrusted, scheme-validated, and capped (`WEB_FETCH_MAX_CHARS`) |
| `evaluate_app.py` | Strategic teardown of a product from its website URL (`evaluate_app`) — a fixed pipeline (Firecrawl fetch → deterministic compaction → one model call) producing a skeptical VC-style analysis: hidden risks, adoption friction, missing technical constraints. See [docs/app-evaluator.md](docs/app-evaluator.md) |
| `evaluate_against.py` | Evaluate a target (a URL or inline text) against the user's **own** standards (`evaluate_against`) — same fixed pipeline as `evaluate_app`, but the rubric is a wiki "lens" page he curates, loaded at call time. Adding a lens is writing a page, not a code change. See [docs/lenses.md](docs/lenses.md) |
| `github_starred.py` | List starred GitHub repos, optionally filtered to those pushed to since a given timestamp, with a `recent_changes` summary (release notes or recent commit subjects) per matched repo; `fetch_readme` returns a repo's raw README (best-effort, feeds the `/starred` blurbs); `fetch_latest_release` returns a repo's latest published release (best-effort, feeds the `/starred` release-awareness column); `compare_versions` normalizes two tag strings to a numeric core and reports whether an installed version is behind a release (feeds the `/starred` "Installed" column) |
| `strava.py` | Strava activities via the Strava API (own OAuth app), for a given date. Run `--authorize` once to mint a refresh token |
| `calendar.py` | Google Calendar read/write — `get_upcoming_events`, `get_events_by_date` (any past or future range, including the words `'today'`/`'yesterday'`, resolved in Python so the model never guesses a date), `log_calendar_event` (idempotent via `source_id`), and `recolor_event` (takes a category name from `CATEGORY_COLORS`, not a raw colorId). `get_events_in_range` backs the date tools and the colorizer but isn't itself a registered tool |
| `email.py` | Send email via Gmail API (plain text or HTML) |
| `learnings_file.py` | Write a daily learnings review to a Markdown file in the Obsidian vault (`LEARNINGS_DIR`), one file per day, and read one back (`read_entry`, used by `daily_synthesis` to pick up the AI-chat log — it looks one subdirectory down too, since ObsidianWikiAgent files ingested files out of `raw/`) |
| `wiki.py` | Read-only search of the learnings wiki (`WIKI_VAULT_PATH`) so Wren can answer "what did I decide about X" — `read_wiki_index`, `list_wiki_pages`, `read_wiki_page` over the concept pages built by ObsidianWikiAgent, plus `page_summaries` (every page's one-line summary) and `list_project_pages` (the pages marked `project: true`, with the checkout each describes), both used by `daily_synthesis` — not model-facing tools. Reads the vault's `wiki/` dir only; `raw/` is a write-only drop (`LEARNINGS_DIR`) that ObsidianWikiAgent files and summarizes. Requires `WIKI_VAULT_PATH` to exist |
| `projects.py` | The user's local checkouts under `PROJECTS_DIR` — `list_projects` and `read_project`. Deterministic, model-free scan of git freshness plus each project's README, CLAUDE.md and `docs/` headings, **and nothing else** (never `.env`). Both scan live and merge the nightly cache, so quoted git facts are current, not a day stale. See [docs/projects.md](docs/projects.md) |
| `google_tasks.py` | Google Tasks read/write (`get_tasks`, `get_tasks_due_soon`, `create_task`, `update_task_due_date`, `complete_task`) |
| `chrome_history.py` | Read Chrome's local history DB for a date range |
| `youtube.py` | List videos Liked on the authorized YouTube channel in a date range (`fetch_liked_videos`) — title, channel, and description per video, via the YouTube Data API v3 and the shared Google OAuth token. Feeds `daily_youtube_learnings` and `daily_synthesis`, and is a chat tool in the `activity` group alongside `chrome_history.py` and `strava.py` |
| `memory.py` | Persistent long-term memory in two tiers — `remember` (archival, search-only) and `pin` (active, injected into every system prompt), plus `recall`, `recategorize`, `archive`, `forget`. Stored in `config/wren_memory.json`; the writes are confirmation-gated. See [docs/memory.md](docs/memory.md) |
| `skills.py` | Procedural memory (chat-only) — reusable how-to procedures composing the other tools: `list_skills`, `read_skill`, `write_skill` (create/overwrite), `delete_skill`. One Markdown file per skill under `skills/` (override with `WREN_SKILLS_DIR`); a capped title+one-line index is injected into the chat prompt so Wren knows what procedures exist, reading a body on demand. Writes are confirmation-gated |
| `reminders.py` | Scheduled reminders — `set_reminder` (parses the time in Python via `dates.resolve_reminder_time`, not the model), `list_reminders`, `cancel_reminder`. Stored in `config/reminders.json`; the `reminder_sweep` task fires each due one as an `ntfy` phone push, then clears it. Set/cancel are confirmation-gated |
| `schedule.py` | Read-only view of Wren's *own* launchd-scheduled tasks (`list_scheduled_tasks`) — the same schedule/next-run/last-status data the dashboard shows, so chat can answer "what do you run?" / "what's next?". Reuses the `chat.insights` dashboard data layer; distinct from the user's Google Tasks and reminders |
| `background.py` | Background tasks — `run_in_background`, `list_background_jobs`, `get_job_result`. Jobs live in `config/bg_jobs.json`; the `bg_worker` task runs them. Posture is "read/draft freely, tap-to-approve consequential actions". Also owns the HMAC-signed approval tokens. See [docs/background.md](docs/background.md) |
| `opportunities.py` | Opportunity signal store for the fractional-work scout — `list_opportunities`, `update_opportunity` (mark interested/dismissed), `watch_company`/`unwatch_company`. The `opportunity_digest` task fills it; full lifecycle in [docs/opportunity-scout.md](docs/opportunity-scout.md) |
| `research.py` | Company research — a fixed pipeline, not a freeform agent task: bounded Tavily searches summarized into a fixed-template brief. `research_opportunity` enriches a scout item; `research_company` researches any company by name and returns the brief directly. Read-only; web snippets are untrusted display text. See [docs/opportunity-scout.md](docs/opportunity-scout.md) |
| `notify.py` | Phone push via the self-hosted ntfy server (`notify`) — the failure-alert channel every scheduled task reports through, with tap-to-approve action buttons for background jobs and an opt-in `email_fallback` for one-shot alerts. `ntfy_health` probes the server (reachability, not token validity) for the dashboard's push pill. Not a model-facing tool |
| `prose_checks.py` | Deterministic pre-pass checks for evaluation lenses — em dashes per sentence, exact banned phrases — computed in Python and handed to `evaluate_against`'s model call as fact rather than asked of it. Not a model-facing tool; see [docs/lenses.md](docs/lenses.md) |
| `google_auth.py` | Shared OAuth helper — one cached token for Calendar, Gmail, Tasks, and YouTube (read-only) scopes |

Every tool module is runnable standalone for testing, e.g.:
```bash
.venv/bin/python -m agent.tools.weather --location "Boston,MA,US"
.venv/bin/python -m agent.tools.calendar list --hours-ahead 48
```

## Scheduled tasks

| Task | Schedule | What it does |
|---|---|---|
| `tasks/ai_chat_learnings.py` | Daily 4:30 AM | Covers the prior day's chats with AI agents — Claude Code sessions from `~/.claude/projects` plus any Gemini export dropped in `WREN_GEMINI_CHATS_DIR` — into an **Accomplished / Learned** summary per session. Writes `AI-Chat-Learnings-<date>.md` in `LEARNINGS_DIR`. A day with no chats writes nothing; `--backfill N` does the last N days. See [docs/ai-chat-learnings.md](docs/ai-chat-learnings.md). |
| `tasks/claude_time_blocks.py` | Daily 4:45 AM | Logs yesterday's Claude Code working hours to Google Calendar, so the day is on record without anyone remembering to block it out. Pools every session's timestamps into one timeline, splits it on idle gaps, and creates one event per stretch — non-overlapping by construction, colored as work, deduped by `source_id` so re-runs and `--backfill N` never duplicate. A day with no sessions writes nothing. See [docs/claude-time-blocks.md](docs/claude-time-blocks.md). |
| `tasks/daily_youtube_learnings.py` | Daily 5:05 AM | Covers the prior day's YouTube Liked videos — the model writes a short synthesis of what they teach, and the linked list of exact videos is appended in Python so the URLs are always real. Writes `Daily-YouTube-<date>.md` in `LEARNINGS_DIR`. A day with no Likes writes nothing. See [docs/daily-learnings.md](docs/daily-learnings.md). |
| `tasks/daily_chrome_learnings.py` | Daily 5:15 AM | Covers the prior day's Chrome browsing into a compact daily log — **Tools & Tech Encountered** plus **Product & Strategy** — written as `Daily-Chrome-<date>.md` in `LEARNINGS_DIR`. Each site carries its top page *paths*, so the review says what was being looked into rather than restating tab titles. Excluded domains/keywords come from [preferences](docs/preferences.md). A quiet day writes nothing; a failed vault write emails the draft instead. See [docs/daily-learnings.md](docs/daily-learnings.md). |
| `tasks/project_scan.py` | Daily 5:30 AM | Refreshes the local project registry that feeds `daily_synthesis`'s project anchors. Scans each checkout under `PROJECTS_DIR` for git freshness plus its README, CLAUDE.md and `docs/` headings, **and nothing else** — never `.env`. One model call per project distils that into a summary and topic terms, cached in `config/projects.json` and keyed on a document hash, so a commit touching no docs costs nothing. `--refresh` regenerates all. See [docs/projects.md](docs/projects.md). |
| `tasks/daily_synthesis.py` | Daily 5:45 AM | Proactive synthesis: matches yesterday's browsing, YouTube Likes and AI-chat *Learned* bullets against his own projects, his wiki pages and watched companies, then has the model keep only the genuine, non-obvious connections. Pushes at most 3 "these line up — want a summary?" nudges via ntfy and writes `Daily-Synthesis-<date>.md` to `SYNTHESIS_DIR` (default `<vault>/nudges`, deliberately not the vault's `raw/`). **Silence is the common case.** See [docs/daily-synthesis.md](docs/daily-synthesis.md). |
| `tasks/strava_download.py` | Daily 5:50 AM | Fetches yesterday's Strava activities and maps each one onto a Google Calendar event in plain Python — no model, since it's a pure field mapping with no natural-language step. Deduped by `source_id` (Strava activity id) so re-runs never create duplicates. |
| `tasks/morning_brief.py` | Daily 6:00 AM | Fetches weather + next-24h calendar events + Google Tasks past due or due within 48h (via `google_tasks.get_tasks_due_soon`) + starred GitHub repos pushed to since the last brief (via `github_starred.fetch_starred_repos`, cursor persisted in `config/github_starred_state.json`), has the model write a short "at a glance" summary and a one-sentence intro for the starred-repos section, assembles a styled HTML email (weather / calendar / tasks due soon / Starred Repos sections), sends it via Gmail. The pipeline lives in `build_and_send_brief()`, shared with the chat `send_morning_brief` tool. |
| `tasks/opportunity_digest.py` | Weekly Sundays at 9:00 PM | The fractional-work opportunity scout. Polls three free, ToS-clean sources — SEC Form D filings in the watched states, leadership openings on watched ATS boards, and the current HN "Who is hiring" thread — dedupes into `config/opportunities.json`, scores fractional-operator fit, and emails a three-section digest. **Nothing new → no email.** See [docs/opportunity-scout.md](docs/opportunity-scout.md). |
| `tasks/starred_blurbs.py` | Weekly Sundays at 8:00 PM | Caches a one-line "what it does" blurb per starred GitHub repo for the `/starred` view — one model call summarizing its README, cached in `config/starred_blurbs.json`, falling back to the repo's GitHub description. Only newly-starred repos each run; de-starred repos are pruned; `--refresh` regenerates all. See [docs/starred.md](docs/starred.md). |
| `tasks/starred_releases.py` | Daily 8:00 PM | Caches each starred repo's latest published release in `config/starred_releases.json` for the `/starred` view's release-awareness column. No model — pure GitHub reads. The cache is rewritten from the live star list each run, so de-starred repos are pruned. Daily, not weekly, so a new version shows up promptly. See [docs/starred.md](docs/starred.md). |
| `tasks/starred_installed.py` | Daily 8:10 PM | Resolves the installed version of each repo tracked in the hand-edited `config/starred_installed.json`, caching results in `config/starred_installed_versions.json` for the `/starred` view's "Installed" column. Runs each `version_cmd` locally with no shell and a timeout; no model. See [docs/starred.md](docs/starred.md). |
| `tasks/log_inspector.py` | Daily 8:00 AM | Watches Wren's own logs — the only task that does. Scans the last 24h of `logs/*.log` for errors and the small model's strain signals, and separately checks via `parse_runs()` that every scheduled task actually ran and finished, catching what a line scan can't see. Pure Python, no model: a health check that called the model couldn't report that the model is down. **Quiet by default — a push always means something needs attention.** See [docs/log-inspector.md](docs/log-inspector.md). |
| `tasks/calendar_colorizer.py` | Daily 5:00 PM | Fetches yesterday's calendar events, has the model guess a category per event title (the categories from `config/preferences.json` — Work, Fitness, Meal Prep, Domestic/Chores, Meetings, Travel, and so on) and returns a colorId per event, then patches each event's color. Always re-classifies, even events colored by a previous run or by hand — except the `claude_time_blocks` entries, which arrive already colored. On failure, pushes a phone alert and emails a notice. |
| `tasks/reminder_sweep.py` | Every 60s (poll) | Fires any reminder that has come due as an `ntfy` phone push, then clears it, so a reminder lands within about a minute of its time. No model — the time was resolved in Python by `dates.resolve_reminder_time` when `set_reminder` saved it. |
| `tasks/bg_worker.py` | Every 30s (poll) | Runs one queued background job per invocation from `config/bg_jobs.json` through the full `advance()` loop, pausing any consequential action for tap-to-approve phone approval and retrying transient failures. The agent stack is imported lazily so the idle poll — the overwhelmingly common case — stays cheap. See [docs/background.md](docs/background.md). |

The last two are **interval pollers** (`StartInterval`, not
`StartCalendarInterval`). The dashboard deliberately classifies them as daemons:
they get no **Run now** button — a hand-spawned poller could race launchd's copy
and pick up the same job twice — and their poll-shaped logs aren't parsed as run
history (`is_daemon` in `chat/insights.py`).

All of these are **fully unattended** — no prompts, no approval steps. This is a
deliberate difference from the original interactive Claude Code skills these
were ported from (`ai-memory` / `AgentOS`), which asked clarifying questions
and waited for approval before writing anywhere. There's nobody to ask at
5am, so these versions infer everything from the data and just act.

**Failure alerts (push).** Since these run with nobody watching — and a
browser tab can't reach out while an emailed failure notice gets buried — each
task pushes a one-line alert to the user's phone if its run fails, via a
self-hosted [ntfy](https://ntfy.sh) server (`agent/tools/notify.py`). The push
is best-effort: a notify outage is logged and swallowed so it can never mask
the underlying task failure. Alerts that fire once and are gone if they don't
land — `notify_failure` and the log inspector's rollup — pass
`email_fallback=True`, so a dead push channel can't silently swallow them.
It's opt-in per call rather than the default because `reminder_sweep` retries a
failed push every 60s, which would turn an outage into thousands of emails.
It's failures-only (no "success" pings), and
leaving `NTFY_URL` unset simply disables it. Self-hosting on the always-on Mac
mini behind Tailscale keeps alerts private and off the public internet;
`auth-default-access: deny-all` plus a publish token means nobody else can
inject fake notifications. Set `NTFY_URL` and `NTFY_TOKEN` in `config/.env`
(see Setup).

The dashboard header carries a live `ntfy up` / `ntfy down` pill so you can tell
the channel is alive without waiting for the 8am log inspector's check. It
probes ntfy's health endpoint, so it reports that the **server is reachable** —
not that `NTFY_TOKEN` is still valid for the topic. A revoked token shows `ntfy
up` and still fails every publish; testing that would mean sending a real push.

## Wren — ad hoc chat

`chat/server.py` is a small always-on Flask app so the user can talk to Wren
directly instead of waiting for a scheduled run — check something, ask a
question, or have her take an action right now.

```
chat/
  server.py                # Flask app: auth, conversation state, chat routes
  auth.py                  # the shared _authenticated() session check
  login_throttle.py        # per-client failed-login limiter
  routes_dashboard.py      # read-only dashboard/scheduler JSON API (blueprint)
  routes_opportunities.py  # opportunity triage API (blueprint)
  routes_starred.py        # starred-repo API, live list + cached fallback (blueprint)
  routes_games.py          # games API, hosted bundles, AI proxy (blueprint)
  routes_logs.py           # log-viewer JSON API (blueprint)
  insights.py              # no-Flask data layer: plists, logs, capabilities, /map
  logview.py               # no-Flask log reader behind routes_logs.py
  static/index.html        # single-page chat UI (vanilla JS, no build step)
```

The conversation engine and auth stay in `server.py`; every feature API is a
Flask blueprint, and `insights.py` and `logview.py` import no Flask at all so
they stay unit-testable and runnable standalone.

- **Tools available in chat:** the whole registry in `agent/toolset.py` — the
  same tools listed in the [`agent/tools/` table](#agenttools--one-module-per-capability)
  above. A session doesn't receive them all at once: it gets a small
  always-loaded core (weather, calendar, tasks, web search, memory, reminders,
  skills-read) plus whichever groups the turn pulls in by keyword or via the
  model's `load_tools` meta-tool, so the small model's context isn't crowded by
  schemas it won't use. Which tools sit in which group, and how a group loads,
  is in [docs/tool-loading.md](docs/tool-loading.md). Three chat-only details
  worth knowing here:
  - **Dates are resolved in Python, never by the model.** `get_events_by_date`
    takes the words `'today'`/`'yesterday'`, `fetch_starred_repos` takes a
    `days_ago` integer, and `set_reminder` takes a time phrase verbatim ("in 2
    hours", "tomorrow 9am") — each resolved by `agent/dates.py`, because a small
    local model guesses dates badly. Likewise `recolor_event` takes a category
    *name* from `CATEGORY_COLORS`, not a raw colorId.
  - **Three kinds of durable state, deliberately separate.** *Memory* is facts
    ([docs/memory.md](docs/memory.md)) — two tiers, where only pinned facts enter
    every system prompt. *Skills* are procedures (`skills/*.md`), with a capped
    index injected each turn and bodies read on demand. The *learnings wiki* is
    external notes (`WIKI_VAULT_PATH`), read-only, and only its `wiki/` dir —
    `raw/` is a write-only drop that ObsidianWikiAgent ingests.
  - **Long jobs can be handed off.** `run_in_background` detaches a multi-step
    task that outlives the chat turn and pushes a summary when done, pausing
    consequential actions for phone approval
    ([docs/background.md](docs/background.md)).

  Not yet wired up for chat: writing a learnings review file —
  `learnings_file.write_entry` has no `TOOL_SCHEMA` (it's only ever called
  directly from Python by the daily learnings tasks). The same pattern extends
  it later if wanted.
- **Confirmation gate:** before any state-changing tool runs — those in
  `toolset.WRITE_TOOLS`, the single source of truth (the calendar, Google Tasks,
  reminder, skill, and memory writes — `remember`/`pin`/`recategorize`/`forget` —
  plus `send_email`, `send_morning_brief`, `send_opportunity_digest`, the
  opportunity-store writes, and `run_in_background`) — the chat UI shows
  what Wren wants to do and waits for a tap to confirm or cancel — enforced in
  code (`advance()`'s `confirm_before` set in `agent/loop.py`), not just
  requested in the prompt, so it doesn't depend on the small local model
  reliably remembering to ask.
- **Session memory:** in-memory only, per browser session (a signed cookie
  carries a session id; conversation history lives in a server-side dict
  keyed by it). Fresh conversation on first visit or after tapping "New
  chat"; lost entirely on server restart — no persistence across sessions.
  Long sessions are trimmed oldest-turn-first to stay inside the model's
  context window (`WREN_CHAT_MAX_HISTORY_CHARS`, see above).
- **Auth:** a shared token (`WREN_CHAT_TOKEN` in `config/.env`) gates
  `POST /login`, checked with a constant-time comparison; a signed session
  cookie (`FLASK_SECRET_KEY`) remembers you for 30 days after that. This is
  defense-in-depth — the real security boundary is that the server is only
  reachable over Tailscale (below), never the open internet.
- **Transport:** `tailscale serve` terminates HTTPS for the tailnet hostname
  and reverse-proxies to the plain-HTTP Flask app on `127.0.0.1:8420` — see
  "Running it" below. The session cookie is `Secure`, so it's only sent over
  that HTTPS origin; a direct `http://<tailscale-ip>:8420` request will load
  the login page but won't keep you signed in.
- **Not a production server:** `chat/server.py` runs Flask's built-in dev
  server (`app.run(...)`), which is fine here — single user, LAN/Tailscale-
  only, and `launchd`'s `KeepAlive` already handles the process-supervision
  job a "real" WSGI server setup would otherwise be for.

### Running it

Unlike the scheduled tasks (`StartCalendarInterval`), this needs to just stay
running — `launchd/local.wren.wren.plist` uses `RunAtLoad` +
`KeepAlive` instead, so it starts on login and restarts if it crashes. Same
log convention as the other tasks: `logs/wren.log` (structured) +
`logs/wren.launchd.log` (raw stdout/stderr).

To reach it from your phone from anywhere (not just home WiFi), install
[Tailscale](https://tailscale.com) on the Mac Mini and your phone, signed
into the same account on both — this creates a private encrypted network
between just your devices, so the chat server never needs to be exposed to
the public internet or have a router port forwarded.

Tailscale's WireGuard tunnel already encrypts traffic between devices, but
`tailscale serve` adds a real HTTPS front door on top (auto-issued/renewed
Let's Encrypt cert for the tailnet hostname, no cert management needed) —
set up once per machine:

```bash
# one-time: enable "HTTPS Certificates" for the tailnet at
# https://login.tailscale.com/admin/dns, then:
tailscale serve --bg 8420
tailscale serve status   # confirm it's proxying to the chat server
```

This persists across reboots (`tailscaled` reapplies it on start) — no
launchd entry needed. Once both devices are on your tailnet, reach the chat
at `https://<mac-mini-tailscale-name>/` (port 443, the HTTPS default — no
`:8420` needed, `tailscale serve` handles the mapping) from anywhere your
phone has a connection.

## Dashboard

The same always-on chat server also serves a dashboard at
`http://127.0.0.1:8420/dashboard` (or the Tailscale HTTPS URL) — one place to
see what's scheduled, whether it's working, and what Wren can do. It reuses the
chat server's token auth, so no separate login.

```
chat/
  insights.py         # read/parse layer: plists -> schedules, logs -> runs,
                      # tool schemas -> capabilities, plus the run-now manager
  static/dashboard.html  # single-page dashboard UI (vanilla JS, no build step)
  static/run-chart.js # the run-duration charts (hand-rolled SVG, no library)
  static/favicon.svg  # Wren mark (cream wren on terracotta) — header logo + favicon
  static/{favicon-32,apple-touch-icon,icon-512}.png  # raster fallbacks
```

It's a single scrollable page (no tabs). Chat lives at `/`, not here:

- **Run duration** (top of the page) — one small chart per task, plotting how
  long each of its runs took over the last 30 days (capped at the 30 most recent
  runs, past which the points overlap into a smear), with the median as a dashed
  reference line and failures in red. This is the question the dot-strip below
  can't answer: a task's runs are near-uniformly green, but `morning_brief` has
  taken 8 seconds and it has taken 25 minutes, and `ai_chat_learnings` once ran
  for 56 — all of them "success". Hand-rolled SVG in
  `chat/static/run-chart.js` (no charting library, so the page still draws
  itself offline), on a log scale because durations span three orders of
  magnitude within a single task. See [docs/run-charts.md](docs/run-charts.md).
- **Scheduled tasks** (below it) — a table, one row per task: name, schedule,
  next run, last-run status (✓/✗/running) with a dot-strip of recent runs, and
  **Run now** / **See runs**. Nothing new is persisted: schedules are read live
  from `launchd/*.plist` and run history is parsed from `logs/<task>.log`
  (rotated backups included) using the loggers' own `Starting … run` /
  `… run complete` / `… run failed` markers. The always-on chat server is a
  muted last row (no Run now). The table also carries the three
  ObsidianWikiAgent jobs that maintain the learnings wiki — they run from a
  sibling repo, so they report here but have no Run now button. See
  [docs/external-tasks.md](docs/external-tasks.md).
- **Learnings wiki** (below the table) — page count, how many raw sources are
  waiting to be filed and how long the oldest has waited, and the vault's last
  backup with any unpushed commits. This is the question the task rows can't
  answer: an ingest that skips a source still logs `run complete` and still
  shows green, so a file can sit unfiled for days behind a healthy-looking row.
  Read live from the vault, not from the logs. See
  [docs/external-tasks.md](docs/external-tasks.md).
- **Run detail** — clicking a row (or **See runs**) opens a right-side
  slide-over with the task's run history; click a run for its tool-call timeline
  (`name → args → result`) and the final response or the error/traceback.
- **Capabilities** (bottom) — auto-generated from the chat
  tools' `TOOL_SCHEMA`s as chips, grouped read-only (Wren runs them itself) vs.
  the confirmation-gated write tools. Click a chip to see its parameters. Always
  in sync with what's actually registered — no separate docs to maintain.
**Run now runs the real task, side effects and all** — `morning_brief` sends the
actual email, `strava_download`/`calendar_colorizer`/`claude_time_blocks` write
real calendar events —
exactly what the schedule does, just now. The button asks for a click-through
confirm and refuses a second concurrent run of the same task. It's a read-only
window otherwise: schedules are still edited by hand in the `.plist` files
(see below), not from the UI.

New dashboard routes on the chat server, all behind the same auth:
`GET /dashboard`, `GET /api/schedules`, `GET /api/runs/<task>`,
`GET /api/runs/<task>/<run_id>`, `GET /api/capabilities`,
`GET /api/run_stats`, `GET /api/health/ntfy` (the header's push-channel pill),
`GET /api/system_map` (the `/map` payload), `GET /api/memories` (the `/memories`
tables), `POST /api/run/<task>`, `GET /api/run/<task>/status`. All of them live
in `chat/routes_dashboard.py`.

### Logs

`http://127.0.0.1:8420/logs` reads any file under `logs/` as a formatted stream,
**newest first** (same auth as the dashboard): a severity rail and tinted rows
for warnings and errors, the date hoisted onto a day divider, continuation lines
and oversized payloads folded behind an expander, and `key=value` /
`name(args) -> result` highlighted. Filter by level or text; a filtered view
keeps two rows of context either side of each hit, because the cause of a
warning usually sits above it. **Live** (on by default) polls every 4 seconds
and puts new lines on top without moving your scroll position.

This is the only way to read the **chat server's own log** — the dashboard's run
drawer covers scheduled-task runs and refuses daemons. Both files per task are
reachable: the structured log and launchd's stdout capture, which is where a
crash before the logger initialises lands.

Reads are bounded to a 512 KB window from the end of the file with a `load older`
cursor, so the counts describe the window read, not the whole file — the caption
says which. Backed by `GET /api/logs` and `GET /api/logs/entries` in
`chat/routes_logs.py`, over the reader in `chat/logview.py`. See
[docs/logs.md](docs/logs.md).

### Memories

`http://127.0.0.1:8420/memories` is a read-only view of `memory.py`'s store
(same auth as the dashboard): an **active** table (pinned facts always in the
system prompt) and an **archival** table (search-only facts), each with
category and created date. Archival is sorted by `access_count` descending —
the facts that actually get recalled float to the top. Managing memories
(pin/archive/forget) is still chat-only; this page is just for seeing what's
there. Backed by `GET /api/memories`, which calls `memory.recall()` with no
query so viewing the page never bumps any access counts.

### Opportunities

`http://127.0.0.1:8420/opportunities` is the triage surface for the opportunity
scout (same auth as the dashboard) — the digest email is read-only, so this is
where leads actually get worked: **To triage**, **Interested** (the live
pipeline, where research briefs land), and **Watchlist**. Backed by
`GET /api/opportunities` plus small POST/DELETE triage endpoints that call the
same `agent/tools/opportunities.py` store functions the chat tools use, so the
page and chat can't drift apart; each digest email footer links here via
`WREN_PUBLIC_URL`. Triage semantics, how retired openings are handled, and the
rest of the scout's lifecycle: [docs/opportunity-scout.md](docs/opportunity-scout.md).

### Starred

`http://127.0.0.1:8420/starred` (same auth as the dashboard) tables the user's
starred GitHub repos — **Repo · Language · What it does · Latest release ·
Installed** — sorted by most-recently-pushed. `GET /api/starred` fetches the repo
list live and merges in three caches written by scheduled tasks
(`starred_blurbs`, `starred_releases`, `starred_installed`), so the model never
runs on the page's request path. When GitHub is unreachable the list falls back
to its own nightly cache and the page says so, rather than going blank.
Installed-version tracking is opt-in per repo,
and Wren never runs an upgrade — this is the awareness layer, not an installer.
See [docs/starred.md](docs/starred.md).

### Games

`http://127.0.0.1:8420/games` (same auth as the dashboard) lists the games Wren
hosts, each with a Play link and whether it's playable right now; the `list_games`
chat tool answers the same from a message ("what can we play?"). Wren implements
no game — each lives in its own repo, and Wren serves its built bundle under her
own origin and proxies its AI calls to a loopback service, so the game inherits
the chat token instead of needing a tailnet port of its own. One game is
registered today: **Weigh Anchor**, a word-deduction card game whose AI seats
think with the same local model chat uses (so game turns and chat turns queue
behind each other) and which is *cooperative* at two seats by design. Adding a
game is a registry entry plus a plist. See [docs/games.md](docs/games.md).

### System map

`http://127.0.0.1:8420/map` is an explorable radial visualization of the whole
agent (same auth as the dashboard) — Wren at the center, then concentric rings
working outward:

- **Skills** — one node per `skills/*.md` procedure, on a slowly rotating
  dotted ring; click for the full step-by-step body.
- **Memory** — a dot field grouped and color-coded by memory category, plus a
  dimmer band of learnings-wiki page names (empty if the vault dir is
  missing); click a dot for the fact.
- **Routines** — the scheduled tasks, each with its schedule and a last-run
  status dot; hovering one lights up gold edges to the applications it talks to.
- **Applications** — hexagons for the external services the chat tools are
  grouped under, with a satellite dot per tool (hollow diamonds mark
  confirmation-gated write tools).

Clicking any node fills the detail panel, with links into `/dashboard` and
`/memories`. Backed by `GET /api/system_map`, aggregated in
`chat/insights.py:system_map()`.

**Maintenance note:** two hand-maintained maps in `chat/insights.py` need a
one-line update when the agent grows — `TOOL_SERVICES` (tool → service grouping)
and `ROUTINE_USES` (routine → services edges). A drift-guard test fails if a
registered tool is unmapped; an unmapped one would otherwise just land under
"Other".

## Scheduling — launchd

Each task (and the always-on chat server) has a `.plist` in `launchd/`,
installed with `./launchd/install.sh`, which substitutes the path placeholders
and `bootstrap`s each agent into `gui/$(id -u)`.
`launchd` was chosen over `cron` because it survives sleep/wake and is the
native macOS mechanism — no Claude Code/Cowork involvement in scheduling at
all. The calendar-driven tasks use `StartCalendarInterval`; Wren's chat server
(`local.wren.wren.plist`) uses `RunAtLoad` + `KeepAlive`
instead, since it needs to just stay running rather than fire on a timer. The
reminder sweep (`local.wren.remindersweep.plist`) uses a
60-second `StartInterval` — a short-lived poll that fires any due reminders and
exits, so a reminder lands within about a minute of its time. The background
worker (`local.wren.bgworker.plist`) likewise uses a 30-second
`StartInterval`, running at most one queued background job per poll; launchd
never runs two copies of the same job at once, so a long job just delays the
next poll rather than overlapping. The worker's idle poll is deliberately
cheap: the agent stack (Google client libraries etc.) is imported lazily, only
when a poll actually finds a job to run.

The worker is also resilient to the realities of a local model host: a
*transient* error (Ollama restarting, a network blip) leaves the job actionable
and the next poll retries it — up to 3 attempts before it's marked failed — and
a resolved approval is persisted before the run continues, so a retry can never
re-execute an already-approved consequential action. A job stuck in
`awaiting_approval` longer than the 1-hour token lifetime (missed push, expired
buttons) gets its approval push re-sent with fresh tokens, once per lifetime;
as a last resort `python -m agent.tools.background --approve <job_id>` /
`--deny <job_id>` resolves it from the terminal.

Useful commands:
```bash
# check status
launchctl print gui/$(id -u)/local.wren.<name>

# trigger a task on demand (bypasses the schedule, useful for testing)
launchctl start local.wren.<name>

# reload after editing a .plist (boots out the old one, then bootstraps)
./launchd/install.sh launchd/local.wren.<name>.plist
```

The plists in `launchd/` carry `__WREN_ROOT__` and `__HOME__` placeholders
rather than absolute paths, so they're checkout-independent — `install.sh`
substitutes both and copies the result into `~/Library/LaunchAgents/`. Don't
`cp` a plist into place by hand; launchd expands neither `~` nor `$HOME` in
`ProgramArguments`, so an unsubstituted plist fails to start.

Logs land in two places per task: `logs/<name>.log` (structured, written by
the Python task itself) and `logs/<name>.launchd.log` (raw stdout/stderr,
mostly useful if the Python process fails to start at all).

## Setup (from scratch on a new machine)

1. Install [Ollama](https://ollama.com) and pull a tool-calling-capable model
   (e.g. `ollama pull gemma4`). Verify it's running: `ollama list`.
2. Create the venv (needs Python 3.10+):
   ```bash
   python3.12 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
3. Copy `config/.env.example` to `config/.env` and fill in:
   - `OPENWEATHERMAP_API_KEY` — [openweathermap.org](https://openweathermap.org/api)
   - `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `STRAVA_REFRESH_TOKEN` — create a
     Strava API application at [strava.com/settings/api](https://www.strava.com/settings/api)
     (set the Authorization Callback Domain to `localhost`); the owning account must
     have an active Strava subscription or the API returns "Application Inactive". Then
     run `python -m agent.tools.strava --authorize` once and follow the prompts to mint
     the refresh token (requests the `activity:read_all` scope so private activities are
     included).
   - `TAVILY_API_KEY` — [tavily.com](https://tavily.com) (used for web search)
   - `GITHUB_TOKEN` — a GitHub personal access token, used to list starred repos.
     [Fine-grained](https://github.com/settings/personal-access-tokens/new) with
     just the **Starring: Read-only** account permission is the least-privilege
     option; a scopeless [classic](https://github.com/settings/tokens/new) token
     also works for public stars (add the `repo` scope only if you star private
     repos too).
   - `LEARNINGS_DIR` — directory the daily learnings Markdown files are written to
     (defaults to `~/Documents/llm-wiki-learnings/raw`)
   - `SYNTHESIS_DIR` — directory the daily synthesis nudge archive is written to
     (defaults to `~/Documents/llm-wiki-learnings/nudges`). Keep it out of
     `LEARNINGS_DIR`: ObsidianWikiAgent ingests `raw/` as sources, and a nudge
     ingested as a source becomes a fabricated wiki claim. The dir must exist
   - `WIKI_VAULT_PATH` — root of the Obsidian vault Wren reads to answer "what did
     I decide about X" via the `wiki.py` tools (defaults to
     `~/Documents/llm-wiki-learnings`; the wiki itself is built by ObsidianWikiAgent)
   - `PROJECTS_DIR` — where the user's project checkouts live (defaults to
     `~/Projects`). Each direct subdirectory is scanned for git freshness plus its
     README, CLAUDE.md and `docs/` headings — and nothing else, never `.env` —
     to build the project anchors `daily_synthesis` matches the day against.
     See [docs/projects.md](docs/projects.md)
   - `GOOGLE_TASKLIST_ID` — optional. By default `get_tasks`/`get_tasks_due_soon`
     read across every Google Tasks list on the account (tasks are commonly split
     across several named lists); set this to a specific list id to scope reads
     — and the default list new tasks land in — to just that one list
   - `BRIEF_TO_EMAIL`, `DEFAULT_LOCATION` — your own values
   - `NTFY_URL`, `NTFY_TOKEN` — optional; enable phone push alerts when a
     scheduled task fails. `NTFY_URL` is the full topic URL of a self-hosted
     [ntfy](https://ntfy.sh) server (e.g.
     `http://<mac-mini-tailscale-name>:2586/wren-alerts`), `NTFY_TOKEN` is a
     publish token for that topic. See "ntfy push server" below. Leave unset
     to disable push.
   - `WREN_CHAT_TOKEN`, `FLASK_SECRET_KEY` — generate each with
     `python -c "import secrets; print(secrets.token_hex(32))"`; leave
     `WREN_CHAT_PORT` at its default unless it conflicts with something else
   - `WREN_PUBLIC_URL` — optional; the chat server's public HTTPS base over
     Tailscale (from `tailscale serve`, e.g.
     `https://<mac-mini-tailscale-name>.ts.net`). Used to build the
     tap-to-approve buttons on background-task approval pushes. If unset, those
     pushes still arrive but without buttons
4. Google OAuth setup (Calendar + Gmail + Tasks + YouTube):
   - [console.cloud.google.com](https://console.cloud.google.com/) → create/select a project
   - Enable the **Google Calendar API**, **Gmail API**, **Google Tasks API**, and **YouTube Data API v3**
   - OAuth consent screen → External → add yourself as a test user
   - Credentials → Create Credentials → OAuth client ID → **Desktop app**
   - Download the JSON as `config/google_credentials.json`
   - Run `.venv/bin/python -m agent.tools.google_auth` — opens a browser once, caches `config/google_token.json`. If the account has multiple YouTube channels, **pick the one whose Likes you want at the consent picker** — the API reads whichever channel you select. Note: some YouTube **brand** accounts are blocked from third-party API access (the flow dead-ends at `accounts.google.com/info/servicerestricted`), so you may have to use a personal channel. (Adding the YouTube scope later means deleting `config/google_token.json` and re-running this to re-consent.)
5. Test each task manually before trusting the schedule:
   ```bash
   .venv/bin/python -m tasks.morning_brief
   .venv/bin/python -m tasks.strava_download
   .venv/bin/python -m tasks.daily_chrome_learnings
   .venv/bin/python -m tasks.daily_youtube_learnings
   .venv/bin/python -m tasks.daily_synthesis
   .venv/bin/python -m tasks.calendar_colorizer
   ```
6. Test the chat server manually: `.venv/bin/python -m chat.server`, then
   visit `http://localhost:8420` and log in with `WREN_CHAT_TOKEN`. (Fine for
   a quick smoke test since the session cookie is `Secure` — a real browser
   may not persist login over plain `http://`; use the HTTPS tailnet URL from
   step 8 for anything beyond a one-off check.)
7. Load the launchd plists with `./launchd/install.sh` (see Scheduling section
   above) — this installs every agent in `launchd/`, including
   `local.wren.wren.plist`, the always-on chat server. The script fills in the
   `__WREN_ROOT__` / `__HOME__` placeholders from wherever you checked the repo
   out, so nothing needs hand-editing; rename the `local.wren.*` labels to taste
   if you'd rather they were namespaced to you.
8. Install [Tailscale](https://tailscale.com) on the Mac Mini and your phone
   if you want to reach Wren's chat from outside your home WiFi, then run
   `tailscale serve --bg 8420` (see "Wren — ad hoc chat" above for the
   one-time HTTPS Certificates setup this needs).
9. **ntfy push server** (optional — enables the failure alerts above).
   ntfy has no native macOS server, so it runs as a Linux container under
   colima, kept alive by `launchd/infra/local.wren.colima.plist` rather than
   `brew services` (which doesn't retry a *failed* start — that cost four days
   of silent downtime once). Full runbook, including the topic ACL and the
   publish token: [docs/ntfy-setup.md](docs/ntfy-setup.md). Then set `NTFY_URL`
   and `NTFY_TOKEN` in `config/.env` and smoke-test:
   ```bash
   .venv/bin/python -m agent.tools.notify --message "hello from Wren" --title "Wren"
   ```

## Adding a new scheduled skill

1. Add any new tool module(s) to `agent/tools/` (a `TOOL_SCHEMA` dict + a
   plain callable function, following the existing modules as a template), then
   register each in `agent/toolset.py` — `TOOLS`, `DISPATCH`, the right gating
   set — and slot it into `CORE_TOOL_NAMES` or a `TOOL_GROUP_NAMES` group so
   chat can offer it. The partition test in `tests/test_toolset.py` fails if you
   skip the slotting; see [docs/tool-loading.md](docs/tool-loading.md).
2. Write `tasks/<name>.py` with a `main() -> int`, using `setup_logger` and
   `notify_failure` from `tasks/_common.py`. Decide upfront whether it needs the
   `advance()`/`resolve()` tool-calling loop (multi-step, like
   `tasks/bg_worker.py`) or `complete_text` (deterministic Python structure +
   one narrative paragraph from the model). Prefer `complete_text`
   where possible — it's far more reliable with a small local model.
3. Write a `.plist` in `launchd/` (copy the closest existing one; keep the
   `__WREN_ROOT__` / `__HOME__` placeholders), then install it:
   `./launchd/install.sh launchd/local.wren.<name>.plist`. Never `cp` it into
   `~/Library/LaunchAgents/` by hand — launchd expands neither `~` nor `$HOME`
   in `ProgramArguments`, so an unsubstituted plist silently fails to start.
4. Run the task manually first and check `logs/<name>.log` before relying on
   the schedule.

## Development / tests

Dev-only dependencies live in `requirements-dev.txt` (currently just `pytest`);
install them into the venv with `.venv/bin/pip install -r requirements-dev.txt`.
Run the suite from the repo root:

```bash
.venv/bin/pytest        # or: .venv/bin/pytest -q
```

The one exception to "this project is Python" is a quartet of shared browser
scripts. `chat/static/chat-dock.js` is the chat page's dock,
`chat/static/run-chart.js` the dashboard's duration charts, and
`chat/static/log-view.js` the `/logs` viewer's renderer (see
[docs/logs.md](docs/logs.md)).
`chat/static/nav.js` renders the top-nav menu on
every view from one canonical list, with `chat/static/nav.css` owning its look
and mobile-wrap behavior — so the menu stays consistent instead of drifting per
page. **Adding a new view?** Give its page three things and it inherits the menu
automatically: `<link rel="stylesheet" href="/static/nav.css">` in the head, a
`<nav id="wren-nav" class="wren-nav"></nav>` mount in the header, and
`<script src="/static/nav.js"></script>` before `</body>`; then add the view to
the `VIEWS` list in `nav.js` so every page links to it. All four have their own
jest/jsdom suite under `tests/` (`chat-dock.test.js`, `run-chart.test.js`,
`log-view.test.js`, `nav.test.js`) — install with `npm install` and run from the
repo root:

```bash
npm test
```

Both suites are pure/offline — they mock the model and network, so no Ollama or
API keys are needed. Nearly every module has a dedicated test file (see
`tests/`), covering error paths and edge cases as well as happy paths; the
deliberate exception is live-API wrapper internals (Google/Strava/Tavily
calls), where only the pure helpers and error mapping are exercised. After
editing the always-on chat server, restart it (it runs under launchd) so the
changes take effect.

Note on privacy: `logs/*.log` records tool arguments and results — calendar
contents, email bodies, browsing history — in cleartext (secrets in tool
*arguments* are redacted, but results are not). The logs are gitignored;
treat them as personal data if you back the directory up elsewhere.

Periodic codebase audits write a dated plan to `docs/reviews/`, which is
gitignored — the plans are a working scratchpad about the repo, not part of it.
A finding still worth acting on belongs in the code or in the doc it's about,
not in a review file.

## Security model / trust boundaries

The threat model is deliberately small: a single user, a Tailscale-only network
surface, and a local model with no outbound API by default. Two boundaries carry
the weight.

**Network.** The server binds to `127.0.0.1` and `tailscale serve` reverse-proxies
to it, so nothing listens on the LAN. Access is a 256-bit token compared in
constant time, plus a backing-off failed-login throttle and a set of hardening
response headers. One endpoint is exempt from the session cookie —
`POST /api/bg/resolve`, which the phone's approval buttons call directly — and is
authenticated instead by an HMAC-signed, ~1h, effectively single-use token.

**Prompt injection.** Untrusted text reaches the model from search results,
fetched pages, GitHub, Chrome history, Strava, and YouTube. The containment rule
is one sentence:

> The model can actuate a **consequential** write only with an explicit human
> "yes" — a tap in chat, or a phone approval for a background task's
> external/irreversible action.

Everything else follows from that: chat gates every write in code (not in the
prompt); the prose scheduled tasks use the tool-free `complete_text` path so
injected text has nothing to actuate; `strava_download` uses no model at all;
rendered output is escaped and scheme-validated; and background runs don't get
the memory/skill writers **at all**, since those feed future system prompts. The
policy is three editable sets in `agent/toolset.py` — `WRITE_TOOLS`,
`CONSEQUENTIAL_TOOLS`, `UNATTENDED_EXCLUDED_TOOLS`.

Note that selecting a cloud backend (`WREN_LLM_BACKEND`, or the frontier-escalation
button) sends that task's data off-device — the opposite of the default posture.

Full detail — the per-layer rationale, the approval-token design, why the
recipient is pinned, and what to hold onto when adding a write tool — is in
**[docs/security-model.md](docs/security-model.md)**.

## What's NOT here

- No Anthropic/Claude API usage anywhere in this codebase — no `anthropic`
  package in `requirements.txt`, and no API call to one at runtime. Claude Code
  was only used to *write* this code. The one place "Claude" appears at runtime
  is `tasks/ai_chat_learnings.py`, which reads Claude Code's **local** session
  logs off disk (`~/.claude/projects`) to summarize the prior day's chats — file
  reads, no network, no API, and it exists precisely *because* there is no API
  for past chats (see [docs/ai-chat-learnings.md](docs/ai-chat-learnings.md)).
  Deleting Claude Code from this machine would end that one daily summary and
  affect nothing else.
- `config/.env`, `config/google_credentials.json`, `config/google_token.json`,
  `config/github_starred_state.json`, and `logs/*.log` are gitignored — they
  contain secrets/tokens and machine-specific state.
