# LocalLLMAgent

A fully local agent system that runs on this Mac Mini, powered by a **local LLM
served by Ollama** — no Anthropic/Claude API calls at runtime, and no Claude
Code/Cowork-managed scheduling. The agent has a name: **Wren**. It works two
ways:

- **Scheduled tasks** (Strava-to-calendar logging, a morning brief email, daily
  activity + YouTube + AI-chat learnings entries) — unattended, triggered entirely
  by macOS `launchd`, no human in the loop.
- **Ad hoc chat** — a small always-on web app (`chat/server.py`) so Craig can
  talk to Wren directly and have her take action on request, from anywhere,
  over Tailscale. See "Wren — ad hoc chat" below.

**Make it yours.** Personal preferences — whose agent this is, calendar
categories and colors, which job titles the opportunity scout flags, default
location — live in the committed `config/preferences.json`, not in Python.
Clone the repo, edit that one file (and the persona Markdown in
`agent/identity.md` / `agent/wren.md`), and Wren serves you instead. Every
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

**The backend is swappable too.** All model calls route through one seam
(`agent/loop.py:_llm_chat`), which defaults to local Ollama but can be pointed at
a cloud model (Gemini) via `WREN_LLM_BACKEND` — globally, or per-task with
`WREN_<TASK>_BACKEND` so chat stays local while a heavy scheduled task uses the
cloud. This sends that task's data off-device, the opposite of the local-first
default, so it's opt-in. See [docs/llm-backend.md](docs/llm-backend.md).

**Chat can escalate a weak answer to a frontier model, by hand.** When Craig
judges a local reply too weak, a "Redo with the frontier model" button re-runs
that turn on a configured cloud backend (`WREN_ESCALATION_BACKEND`, provider-
neutral) — one deliberate, badged, logged tap. There's no automatic router: Craig
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

Chat also trims the *tool* overhead: instead of sending all ~45 tool schemas
every turn, a session sends a small always-loaded core plus whichever tool
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
2. **`agent/identity.md`** — who Craig is and how he wants things written
   (direct, concise, no flattery).

Both are hand-maintained, condensed excerpts of `AgentOS/IDENTITY.md` (Part 2
and Part 1 respectively) — trimmed because the full file includes Claude-Code/
AgentOS-specific content (diagrams, "System 2 Founder") that doesn't apply to
a small local model. A third file, **`agent/wren_chat.md`**, holds
*behavioral* instructions (ask questions when useful, narrate intent before an
action) that only make sense in an interactive session — it's loaded only by
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
| `evaluate_against.py` | Evaluate a target (a URL or inline text) against Craig's **own** standards (`evaluate_against`) — the same fixed pipeline as `evaluate_app`, but the rubric is a wiki "lens" page Craig curates (e.g. his product principles), loaded at call time. Returns where the target aligns, where it falls short, and what to change. A lens is any wiki page with `lens: true` in its frontmatter, so adding one is writing a page — no code change. A lens can also opt into deterministic pre-pass checks (em dashes per sentence, exact banned phrases) computed in Python and handed to the model as fact, rather than asked of it. Marker contract, the injected index, the pre-pass, and how to author a lens in [docs/lenses.md](docs/lenses.md) |
| `github_starred.py` | List starred GitHub repos, optionally filtered to those pushed to since a given timestamp, with a `recent_changes` summary (release notes or recent commit subjects) per matched repo; `fetch_readme` returns a repo's raw README (best-effort, feeds the `/starred` blurbs); `fetch_latest_release` returns a repo's latest published release (best-effort, feeds the `/starred` release-awareness column); `compare_versions` normalizes two tag strings to a numeric core and reports whether an installed version is behind a release (feeds the `/starred` "Installed" column) |
| `strava.py` | Strava activities via the Strava API (own OAuth app), for a given date. Run `--authorize` once to mint a refresh token |
| `calendar.py` | Google Calendar read/write (`get_upcoming_events`, `get_events_in_range`, `log_calendar_event` — idempotent via `source_id`) |
| `email.py` | Send email via Gmail API (plain text or HTML) |
| `learnings_file.py` | Write a daily learnings review to a Markdown file in the Obsidian vault (`LEARNINGS_DIR`), one file per day, and read one back (`read_entry`, used by `daily_synthesis` to pick up the AI-chat log — it looks one subdirectory down too, since ObsidianWikiAgent files ingested files out of `raw/`) |
| `wiki.py` | Read-only search of the learnings wiki (`WIKI_VAULT_PATH`) so Wren can answer "what did I decide about X" — `read_wiki_index`, `list_wiki_pages`, `read_wiki_page` over the concept pages built by ObsidianWikiAgent, plus `page_summaries` (every page's one-line summary, used by `daily_synthesis` — not a model-facing tool). Reads the vault's `wiki/` dir only; `raw/` is a write-only drop (`LEARNINGS_DIR`) that ObsidianWikiAgent files and summarizes. Requires `WIKI_VAULT_PATH` to exist |
| `google_tasks.py` | Google Tasks read/write (`get_tasks`, `get_tasks_due_soon`, `create_task`, `update_task_due_date`, `complete_task`) |
| `chrome_history.py` | Read Chrome's local history DB for a date range |
| `youtube.py` | List videos Liked on the authorized YouTube channel in a date range (`fetch_liked_videos`) — title, channel, and description per video, via the YouTube Data API v3 and the shared Google OAuth token |
| `memory.py` | Persistent long-term memory in two tiers — `remember` (archival, search-only), `pin` (active, injected into every system prompt), `recall` (search either tier, optionally by category), `recategorize` (relabel a fact's category in place, keeping its id/history), `archive` (demote active→archival), `forget` (delete). Stored in `config/wren_memory.json`; archival facts track an `access_count`. The writes (`remember`/`pin`/`recategorize`/`forget`) are confirmation-gated. See [docs/memory.md](docs/memory.md) |
| `skills.py` | Procedural memory (chat-only) — reusable how-to procedures composing the other tools: `list_skills`, `read_skill`, `write_skill` (create/overwrite), `delete_skill`. One Markdown file per skill under `skills/` (override with `WREN_SKILLS_DIR`); a capped title+one-line index is injected into the chat prompt so Wren knows what procedures exist, reading a body on demand. Writes are confirmation-gated |
| `reminders.py` | Scheduled reminders — `set_reminder` (parses the time in Python via `dates.resolve_reminder_time`, not the model), `list_reminders`, `cancel_reminder`. Stored in `config/reminders.json`; the `reminder_sweep` task fires each due one as an `ntfy` phone push, then clears it. Set/cancel are confirmation-gated |
| `schedule.py` | Read-only view of Wren's *own* launchd-scheduled tasks (`list_scheduled_tasks`) — the same schedule/next-run/last-status data the dashboard shows, so chat can answer "what do you run?" / "what's next?". Reuses the `chat.insights` dashboard data layer; distinct from Craig's Google Tasks and reminders |
| `background.py` | Background tasks — `run_in_background` (hand off a multi-step task that runs detached and pushes a summary when done), `list_background_jobs`, `get_job_result`. Jobs live in `config/bg_jobs.json`; the `bg_worker` task runs them. Posture is "read/draft freely, tap-to-approve consequential actions". Also owns the HMAC-signed approval tokens. Full lifecycle, approval flow, and exclusions in [docs/background.md](docs/background.md) |
| `opportunities.py` | Opportunity signal store for the fractional-work scout — `list_opportunities`, `update_opportunity` (mark interested/dismissed), `watch_company`/`unwatch_company`. The `opportunity_digest` task fills it; full lifecycle in [docs/opportunity-scout.md](docs/opportunity-scout.md) |
| `research.py` | Company research — a fixed pipeline (not a freeform agent task): bounded Tavily searches summarized by the model into a fixed-template brief. `research_opportunity` enriches and persists onto a scout item (see [docs/opportunity-scout.md](docs/opportunity-scout.md)); `research_company` researches ANY company by name and returns the brief directly — the general-purpose verb chat and skills compose. Read-only against the outside world; web snippets are treated as untrusted display text |
| `google_auth.py` | Shared OAuth helper — one cached token for Calendar, Gmail, Tasks, and YouTube (read-only) scopes |

Every tool module is runnable standalone for testing, e.g.:
```bash
.venv/bin/python -m agent.tools.weather --location "Boston,MA,US"
.venv/bin/python -m agent.tools.calendar list --hours-ahead 48
```

## Scheduled tasks

| Task | Schedule | What it does |
|---|---|---|
| `tasks/ai_chat_learnings.py` | Daily 4:30 AM | Covers the prior day's chats with AI agents: reads every Claude Code session active that day from `~/.claude/projects` (plus any new Gemini export dropped in `WREN_GEMINI_CHATS_DIR`), and has the local model write a brief **Accomplished / Learned** summary per session — outcomes, not the back-and-forth. Writes one `AI-Chat-Learnings-<date>.md` per day in `LEARNINGS_DIR`, one section per session. A day with no chats (or only empty summaries) writes nothing; same vault-write-fails → email fallback. Neither product has an API for past chats, so the sources are local session logs + a drop folder. `--backfill N` summarizes each of the last N days as a separate file. See [docs/ai-chat-learnings.md](docs/ai-chat-learnings.md). |
| `tasks/daily_youtube_learnings.py` | Daily 5:05 AM | Covers the prior day's YouTube Liked videos: the local model writes a short synthesis of what they teach, and a deterministic, scheme-validated linked list of the exact videos (verbatim titles + URLs) is appended in Python; writes it as `Daily-YouTube-<date>.md` in `LEARNINGS_DIR`. A day with no Likes writes nothing. Same vault-write-fails → email fallback. |
| `tasks/daily_chrome_learnings.py` | Daily 5:15 AM | Covers the prior day's Chrome browsing history, has the local model draft a compact daily log — a **Tools & Tech Encountered** section plus a **Product & Strategy** section for product-management reading — writes it as `Daily-Chrome-<date>.md` in `LEARNINGS_DIR` (Obsidian vault, one file per day). Each site carries its top page *paths*, so the review can say what was being looked into rather than restate the tab title. Sites and pages matching `learnings.excluded_domains` / `learnings.excluded_keywords` ([preferences](docs/preferences.md)) are skipped. Small, focused prompt so the on-device model produces a full draft. A day with no meaningful browsing (or a draft that's just "None") writes nothing. If the write fails — e.g. `LEARNINGS_DIR` points somewhere that doesn't exist — it emails the draft instead so it's never silently lost (and pushes a phone alert). |
| `tasks/daily_synthesis.py` | Daily 5:45 AM | Proactive synthesis: connects the prior day's activity to Craig's own accumulated knowledge. Python matches yesterday's browsing + YouTube Likes + the *Learned* bullets of that day's AI-chat log against his wiki pages (name **and** one-line summary, skipping the dated activity logs) and watched/interesting companies by token overlap, discounting tokens too common across the vault to discriminate (**CONNECTION** candidates), and also matches those signals against *each other across channels* (**ECHO** candidates — the same theme reaching him twice independently in one day). The local model does one bounded pass over the shortlist to keep only genuine, non-obvious connections (dropping coincidental keyword hits), and it pushes at most 3 "these line up — want a summary?" nudges via ntfy (email fallback if the push fails) **and** writes a durable `Daily-Synthesis-<date>.md` to `SYNTHESIS_DIR` (default `<vault>/nudges`) so past suggestions are reviewable in Obsidian — deliberately *not* the vault's `raw/`, which ObsidianWikiAgent ingests as sources. No overlap, or nothing genuine, means no push and no file — silence is the common case. See [docs/daily-synthesis.md](docs/daily-synthesis.md). |
| `tasks/strava_download.py` | Daily 5:50 AM | Fetches yesterday's Strava activities and maps each one onto a Google Calendar event in plain Python — no model, since it's a pure field mapping with no natural-language step. Deduped by `source_id` (Strava activity id) so re-runs never create duplicates. |
| `tasks/morning_brief.py` | Daily 6:00 AM | Fetches weather + next-24h calendar events + Google Tasks past due or due within 48h (via `google_tasks.get_tasks_due_soon`) + starred GitHub repos pushed to since the last brief (via `github_starred.fetch_starred_repos`, cursor persisted in `config/github_starred_state.json`), has the model write a short "at a glance" summary and a one-sentence intro for the starred-repos section, assembles a styled HTML email (weather / calendar / tasks due soon / Starred Repos sections), sends it via Gmail. The pipeline lives in `build_and_send_brief()`, shared with the chat `send_morning_brief` tool. |
| `tasks/opportunity_digest.py` | Weekly Sundays at 9:00 PM | The fractional-work opportunity scout. Polls three free, ToS-clean sources — new SEC Form D filings in the watched states (MA/NH/ME), leadership openings on watched ATS boards (Greenhouse/Lever/Ashby/iCIMS, flagging stalled searches), and the current HN "Who is hiring" thread — dedupes them into `config/opportunities.json`, has the model score fractional-operator fit, and emails a three-section Opportunity Digest (ntfy push for high scores; nothing new → no email). Each run also retires openings that have dropped off a cleanly-polled board, so filled roles stop looking like live leads. Full lifecycle — sources, dedupe/watermark behavior, triage semantics, scoring — in [docs/opportunity-scout.md](docs/opportunity-scout.md). |
| `tasks/starred_blurbs.py` | Weekly Sundays at 8:00 PM | Caches a one-line "what it does" blurb for each starred GitHub repo, for the `/starred` view. One isolated model call per repo summarizes its README (`github_starred.fetch_readme`, truncated) into a plain sentence, cached in `config/starred_blurbs.json` keyed by `full_name`; a missing README or unusable output falls back to the repo's GitHub description. Blurbs are generated once per repo (only newly-starred repos each run) and de-starred repos are pruned; `--refresh` regenerates all. See [docs/starred.md](docs/starred.md). |
| `tasks/starred_releases.py` | Daily 8:00 PM | Caches each starred GitHub repo's latest published release (`github_starred.fetch_latest_release`) in `config/starred_releases.json` keyed by `full_name`, for the `/starred` view's release-awareness column. No model — pure GitHub reads, fanned over a small pool. Repos with no releases get no entry; the whole cache is rewritten from the live star list each run, so de-starred repos are pruned. Daily (not weekly) so a new version shows up promptly. See [docs/starred.md](docs/starred.md). |
| `tasks/starred_installed.py` | Daily 8:10 PM | Resolves the installed version of each repo Craig tracks in `config/starred_installed.json` (a hand-edited map of `full_name` → `version_cmd` to run or a static `version`), caching `{version, source, error}` in `config/starred_installed_versions.json` for the `/starred` view's "Installed" column. Runs `version_cmd`s locally with no shell and a timeout; no model. The cache is rewritten from the config each run, so removing a repo prunes it. See [docs/starred.md](docs/starred.md). |
| `tasks/log_inspector.py` | Daily 8:00 AM | Watches Wren's own logs — the only task that does. Scans the last 24h of `logs/*.log` for errors and for the small model's strain signals (a prompt that overflowed `num_ctx` and lost the system prompt off the front, a generation that hit `num_predict` mid-repetition-loop), and separately checks via `parse_runs()` that every scheduled task actually ran and finished — catching the ones a line scan can't see, like a task that crashed before logging or that launchd never fired. Pure Python, no model: a health check that called the model couldn't report that the model is down. Quiet by default — a push (a counts rollup, since ntfy truncates at 500 chars) always means something needs attention. See [docs/log-inspector.md](docs/log-inspector.md). |
| `tasks/calendar_colorizer.py` | Daily 5:00 PM | Fetches yesterday's calendar events, has the model guess a category per event title (Work/LLC, AARP, Fitness, Meal Prep, Domestic/Chores, Meetings, Travel, Dining Out, Shows/Events, Appointments, or Uncategorized) and returns a colorId per event, then patches each event's color. Always re-classifies, even events colored by a previous run or by hand. On failure, pushes a phone alert and emails a notice. |

All of these are **fully unattended** — no prompts, no approval steps. This is a
deliberate difference from the original interactive Claude Code skills these
were ported from (`ai-memory` / `AgentOS`), which asked clarifying questions
and waited for approval before writing anywhere. There's nobody to ask at
5am, so these versions infer everything from the data and just act.

**Failure alerts (push).** Since these run with nobody watching — and a
browser tab can't reach out while an emailed failure notice gets buried — each
task pushes a one-line alert to Craig's phone if its run fails, via a
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

`chat/server.py` is a small always-on Flask app so Craig can talk to Wren
directly instead of waiting for a scheduled run — check something, ask a
question, or have her take an action right now.

```
chat/
  server.py         # Flask app: auth, conversation state, routes
  static/index.html # single-page chat UI (vanilla JS, no build step)
```

- **Tools available in chat:** `fetch_weather`, `fetch_strava`,
  `get_upcoming_events`, `get_events_by_date` (any past or future date range,
  including the words `'today'`/`'yesterday'` — resolved in Python so the
  model never has to guess the current date), `fetch_chrome_history`,
  `search_web` (Tavily web search for current info the model doesn't already
  know), and `fetch_starred_repos` (list starred GitHub repos, optionally only
  those pushed to in the last N days — a `days_ago` integer the model passes
  through rather than computing a date itself, same reasoning as
  `get_events_by_date`'s `'today'`/`'yesterday'` resolution — each matching
  repo also comes back with a `recent_changes` one-to-two-line summary from
  its latest release notes or recent commit subjects), and the Google Tasks
  reads `get_tasks` / `get_tasks_due_soon` (spanning every task list on the
  account) — all read-only,
  execute immediately — plus `log_calendar_event`,
  `send_email`, `recolor_event`, `send_morning_brief`, and the Google Tasks
  writes `create_task` / `update_task_due_date` / `complete_task`
  (state-changing — see confirmation gate below).
  `recolor_event` takes a category name (`agent/tools/calendar.py`'s
  `CATEGORY_COLORS` — the same mapping `calendar_colorizer.py`'s daily job
  uses), not a raw colorId, since that's far more reliable for a small local
  model. `send_morning_brief` builds and sends the same polished HTML brief the
  scheduled task sends (via the shared `build_and_send_brief()`) — chat is told
  to use it rather than freehand-composing a brief with `send_email`, so the
  formatting never degrades to raw markdown. Wren also has a long-term memory in
  two tiers: `remember` saves a fact to searchable archival storage, while `pin`
  keeps a durable preference or routine always-on. Only *active* (pinned) facts
  are injected into every system prompt (via `with_identity()` →
  `memory.render_memory_block()`), so the prompt stays small as the archive grows;
  scheduled tasks see the active set too. `recall` searches either tier (optionally
  by category) and bumps each archival fact's `access_count`; `archive` demotes an
  active fact back to search-only; `recategorize` relabels a fact's category in
  place, preserving its id, creation date, and access count (so re-filing a fact
  never resets its history). `recall` and `archive` run immediately; the four
  tools that create, alter, or delete a fact — `remember`, `pin`, `recategorize`,
  `forget` — are confirmation-gated so a prompt injection in fetched web content
  can't silently write (or, worse, `pin`) a fact. The two tiers, the gating
  rationale, and the friction tradeoff are in [docs/memory.md](docs/memory.md). Wren can also search Craig's **learnings wiki** (`WIKI_VAULT_PATH`, the
  Obsidian vault ObsidianWikiAgent maintains): `read_wiki_index`,
  `list_wiki_pages`, `read_wiki_page` navigate the concept pages the way that
  project's own query CLI does — read the index, open the relevant page(s),
  answer and cite them. Only the vault's `wiki/` dir is readable; its `raw/` dir
  is a write-only handoff to ObsidianWikiAgent (see the tools table). All
  read-only; they return an error (rather than raise) if the vault dir is
  missing. Beyond facts (memory) and external notes (wiki), Wren keeps
  **skills** — reusable *procedures* for multi-step tasks, composed from the
  other tools (e.g. "trip prep → calendar range + weather + packing notes").
  Each is a Markdown file under `skills/`; a capped title+one-line index is
  injected into the chat prompt (chat-only, like the wiki tools, to protect the
  small `num_ctx`), and Wren opens a body on demand with `read_skill` before
  following it. `write_skill` (create or overwrite) and `delete_skill` are
  confirmation-gated; capture is Craig-initiated, mirroring memory. Wren can also
  set **reminders**: `set_reminder` takes Craig's time phrase verbatim (e.g. "in
  2 hours", "3pm", "tomorrow 9am" — resolved in Python by
  `dates.resolve_reminder_time`, never by the model) plus a message, and the
  `reminder_sweep` task pushes it to his phone via `ntfy` when it comes due, then
  clears it. `list_reminders`/`cancel_reminder` manage pending ones; set and
  cancel are confirmation-gated. Wren can also **hand a task off to run in the
  background** with `run_in_background` — a multi-step job that outlives the chat
  turn and pushes Craig a summary when done (see **Background tasks** below).
  `list_background_jobs`/`get_job_result` report on them. Not yet wired up for chat:
  writing a learnings review file — `learnings_file.write_entry` doesn't have a
  `TOOL_SCHEMA` yet (only ever called directly from Python by the daily learnings
  tasks). Same pattern extends it later if wanted.
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
running — `launchd/com.craigdube.localllmagent.wren.plist` uses `RunAtLoad` +
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
  long each of its runs took over the last 30 days, with the median as a dashed
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
  muted last row (no Run now).
- **Run detail** — clicking a row (or **See runs**) opens a right-side
  slide-over with the task's run history; click a run for its tool-call timeline
  (`name → args → result`) and the final response or the error/traceback.
- **Capabilities** (bottom) — auto-generated from the chat
  tools' `TOOL_SCHEMA`s as chips, grouped read-only (Wren runs them itself) vs.
  the confirmation-gated write tools. Click a chip to see its parameters. Always
  in sync with what's actually registered — no separate docs to maintain.
**Run now runs the real task, side effects and all** — `morning_brief` sends the
actual email, `strava_download`/`calendar_colorizer` write real calendar events —
exactly what the schedule does, just now. The button asks for a click-through
confirm and refuses a second concurrent run of the same task. It's a read-only
window otherwise: schedules are still edited by hand in the `.plist` files
(see below), not from the UI.

New dashboard routes on the chat server, all behind the same auth:
`GET /dashboard`, `GET /api/schedules`, `GET /api/runs/<task>`,
`GET /api/runs/<task>/<run_id>`, `GET /api/capabilities`,
`GET /api/run_stats`, `POST /api/run/<task>`, `GET /api/run/<task>/status`.

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

`http://127.0.0.1:8420/opportunities` is the triage surface for the
opportunity scout (same auth as the dashboard) — the daily digest email is
read-only, so this page is where leads actually get worked: **To triage**
(Interested / Dismiss buttons per item), **Interested** (the live pipeline,
where research briefs land), and **Watchlist** (the boards the scout polls).
Openings that come down from a watched board are retired automatically each
run: untriaged ones drop out of the view, while interested ones stay put with
a "no longer listed" badge, since Craig may already have reached out.
Backed by `GET /api/opportunities` plus small POST/DELETE triage endpoints
that call the same `agent/tools/opportunities.py` store functions the chat
tools use, so the page and chat can't drift apart. Each digest email footer
links here (via `WREN_PUBLIC_URL`). What triage actually does to an item —
and everything else about the scout's lifecycle — is documented in
[docs/opportunity-scout.md](docs/opportunity-scout.md).

### Starred

`http://127.0.0.1:8420/starred` (same auth as the dashboard) is a table of
Craig's starred GitHub repos — **Repo · Language · What it does · Latest release
· Installed** — sorted by most-recently-pushed. Backed by
`GET /api/starred`, which fetches the repo list live and merges in each repo's
cached "what it does" blurb (falling back to its GitHub description for any repo
not yet cached), its cached latest release (badged **🆕 new** when cut within the
last 30 days), and Craig's cached installed version (badged **update available**
when it's behind the latest release). The three caches are written by scheduled
tasks (`tasks/starred_blurbs.py`, `tasks/starred_releases.py`,
`tasks/starred_installed.py`), so the model never runs on the page's request path.
Installed-version tracking is opt-in per repo — Craig lists the repos he has (and
how to read each one's version) in `config/starred_installed.json`; Wren never
runs an upgrade. See [docs/starred.md](docs/starred.md).

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

Clicking any node fills the detail panel (tool descriptions, memory text,
skill bodies, routine last-run status) with links into `/dashboard` and
`/memories`. Backed by `GET /api/system_map`, aggregated in
`chat/insights.py:system_map()`. Two hard-coded maps there need a one-line
update when the agent grows: `TOOL_SERVICES` (tool → service grouping; a
drift-guard test fails if a registered tool is unmapped) and `ROUTINE_USES`
(routine → services edges).

## Scheduling — launchd

Each task (and the always-on chat server) has a `.plist` in `launchd/`,
copied to `~/Library/LaunchAgents/` and loaded with `launchctl load`.
`launchd` was chosen over `cron` because it survives sleep/wake and is the
native macOS mechanism — no Claude Code/Cowork involvement in scheduling at
all. The calendar-driven tasks use `StartCalendarInterval`; Wren's chat server
(`com.craigdube.localllmagent.wren.plist`) uses `RunAtLoad` + `KeepAlive`
instead, since it needs to just stay running rather than fire on a timer. The
reminder sweep (`com.craigdube.localllmagent.remindersweep.plist`) uses a
60-second `StartInterval` — a short-lived poll that fires any due reminders and
exits, so a reminder lands within about a minute of its time. The background
worker (`com.craigdube.localllmagent.bgworker.plist`) likewise uses a 30-second
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
launchctl print gui/$(id -u)/com.craigdube.localllmagent.<name>

# trigger a task on demand (bypasses the schedule, useful for testing)
launchctl start com.craigdube.localllmagent.<name>

# reload after editing a .plist
launchctl unload ~/Library/LaunchAgents/com.craigdube.localllmagent.<name>.plist
cp launchd/com.craigdube.localllmagent.<name>.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.craigdube.localllmagent.<name>.plist
```

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
7. Load the launchd plists (see Scheduling section above) — this now
   includes `com.craigdube.localllmagent.wren.plist`, the always-on chat
   server. On a different machine or checkout path, first edit the absolute
   paths in each plist (`ProgramArguments`, `WorkingDirectory`, the two log
   paths — they hardcode `/Users/craigdube/Projects/LocalLLMAgent`) and
   rename the `com.craigdube.*` labels to taste.
8. Install [Tailscale](https://tailscale.com) on the Mac Mini and your phone
   if you want to reach Wren's chat from outside your home WiFi, then run
   `tailscale serve --bg 8420` (see "Wren — ad hoc chat" above for the
   one-time HTTPS Certificates setup this needs).
9. **ntfy push server** (optional — enables the failure alerts above).
   Self-hosting keeps alerts off the public internet and, with `deny-all`,
   makes fake notifications impossible. **ntfy has no native macOS server** —
   `brew install ntfy` gives a *client-only* binary (no `serve`), and even the
   official `darwin` release is client-only. So on macOS the server runs as the
   Linux container in a lightweight VM (colima — no Docker Desktop needed):
   ```bash
   brew install colima docker
   ```
   **Don't use `brew services start colima` to keep it up.** Homebrew's plist
   sets `KeepAlive.SuccessfulExit=true`, which means *relaunch only after a
   clean exit* — so a colima start that **fails** is never retried. That is
   exactly what happened on 2026-07-11: the Mac rebooted, colima's VM died
   mid-shutdown leaving stale state, the boot-time start failed with `vz driver
   is running but host agent is not` (exit 1), launchd gave up, and the push
   channel was down for four days. Nobody noticed, because nothing happened to
   need pushing. Use the replacement service instead — `KeepAlive=true` (retry
   on failure too) plus a wrapper that clears the stale state a crash leaves
   behind, which a bare retry cannot do:
   ```bash
   brew services stop colima           # hand off from Homebrew, if it's running
   cp launchd/infra/com.craigdube.colima.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.craigdube.colima.plist
   ```
   To stop colima deliberately, unload it — `colima stop` alone won't stick,
   since launchd immediately brings it back:
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.craigdube.colima.plist
   ```
   The `log_inspector` task actively probes this channel every morning, because
   a dead one is invisible to any log scan until something tries to use it.
   Create config + data dirs under your home (colima mounts `$HOME` into the
   VM, so the container can read them) and a `server.yml`:
   ```bash
   mkdir -p ~/ntfy-server/{etc,lib,cache}
   ```
   `~/ntfy-server/etc/server.yml`:
   ```yaml
   base-url: "http://<mac-mini-tailscale-name>:2586"
   listen-http: ":2586"
   auth-file: "/var/lib/ntfy/user.db"
   auth-default-access: "deny-all"      # no anonymous read OR publish
   cache-file: "/var/cache/ntfy/cache.db"
   upstream-base-url: "https://ntfy.sh" # iOS only: relays a *contentless*
                                        # wakeup ping so push is instant; the
                                        # message body is still fetched from
                                        # this server, never ntfy.sh.
   ```
   Run the server container (`--restart always` brings it back when colima
   restarts at login):
   ```bash
   docker run -d --name ntfy --restart always -p 2586:2586 \
     -v ~/ntfy-server/etc:/etc/ntfy \
     -v ~/ntfy-server/lib:/var/lib/ntfy \
     -v ~/ntfy-server/cache:/var/cache/ntfy \
     binwiederhier/ntfy:v2.26.0 serve
   ```
   Create the publisher + subscriber, lock the topic down, and mint a publish
   token (`NTFY_PASSWORD=...` sets passwords non-interactively):
   ```bash
   docker exec -e NTFY_PASSWORD='<wren-pw>'  ntfy ntfy user add wren
   docker exec ntfy ntfy access wren wren-alerts write
   docker exec -e NTFY_PASSWORD='<craig-pw>' ntfy ntfy user add craig
   docker exec ntfy ntfy access craig wren-alerts read
   docker exec ntfy ntfy token add wren    # -> tk_...  set as NTFY_TOKEN
   ```
   Set `NTFY_URL=http://<mac-mini-tailscale-name>:2586/wren-alerts` and
   `NTFY_TOKEN=tk_...` in `config/.env`. On your iPhone, install the ntfy app →
   add a custom server pointing at the same Tailscale URL, log in as `craig`
   (its password), subscribe to `wren-alerts`. Smoke-test end to end:
   ```bash
   .venv/bin/python -m agent.tools.notify --message "hello from Wren" --title "Wren"
   ```
   TLS is unnecessary — the link rides Tailscale's encrypted tunnel.

## Adding a new scheduled skill

1. Add any new tool module(s) to `agent/tools/` (a `TOOL_SCHEMA` dict + a
   plain callable function, following the existing modules as a template).
2. Write `tasks/<name>.py` — decide upfront whether it needs the
   `advance()`/`resolve()` tool-calling loop (multi-step, like
   `tasks/bg_worker.py`) or `complete_text` (deterministic Python structure +
   one narrative paragraph from the model). Prefer `complete_text`
   where possible — it's far more reliable with a small local model.
3. Write a `.plist` in `launchd/`, copy it to `~/Library/LaunchAgents/`, load it.
4. Run the task manually first and check `logs/<name>.log` before relying on
   the schedule.

## Development / tests

Dev-only dependencies live in `requirements-dev.txt` (currently just `pytest`);
install them into the venv with `.venv/bin/pip install -r requirements-dev.txt`.
Run the suite from the repo root:

```bash
.venv/bin/pytest        # or: .venv/bin/pytest -q
```

The one exception to "this project is Python" is a pair of shared browser
scripts. `chat/static/chat-dock.js` is the chat page's dock and
`chat/static/run-chart.js` the dashboard's duration charts.
`chat/static/nav.js` renders the top-nav menu on
every view from one canonical list, with `chat/static/nav.css` owning its look
and mobile-wrap behavior — so the menu stays consistent instead of drifting per
page. **Adding a new view?** Give its page three things and it inherits the menu
automatically: `<link rel="stylesheet" href="/static/nav.css">` in the head, a
`<nav id="wren-nav" class="wren-nav"></nav>` mount in the header, and
`<script src="/static/nav.js"></script>` before `</body>`; then add the view to
the `VIEWS` list in `nav.js` so every page links to it. Both scripts have their
own jest/jsdom suite — install with `npm install` and run from the repo root:

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

## Security model / trust boundaries

The threat model here is deliberately small: a single user, a Tailscale-only
network surface, and a local model with no outbound API. Two boundaries are
worth stating explicitly.

**Network.** The chat/dashboard server binds to `127.0.0.1` (override with
`WREN_CHAT_HOST`). `tailscale serve` reverse-proxies to that loopback address,
so nothing needs to listen on the LAN. Access is gated by a 256-bit hex token
(`WREN_CHAT_TOKEN`) compared in constant time; the token cookie is the only
credential. The token's entropy already makes brute-force infeasible, but
`/login` also applies a per-client failed-attempt throttle (`LoginThrottle` in
`chat/server.py`) as defense-in-depth — a handful of wrong guesses trigger a
short, backing-off lockout. The tuning stays lenient enough that a legitimate
mistyped token doesn't durably lock the single user out. Every response also
carries a set of hardening headers (`Content-Security-Policy`,
`X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`) as defense-in-depth against clickjacking and
any future markup slip — see `_security_headers` in `chat/server.py`.

One endpoint is exempt from the session cookie: `POST /api/bg/resolve`, which a
background-task approval button on the phone calls directly. It's authenticated
instead by an HMAC-signed, ~1h-expiry, effectively single-use token
(`background.make_approval_token`, signed with `FLASK_SECRET_KEY`), and it does
exactly one thing — flip one already-model-proposed job to approved/denied — so
even a leaked token has a bounded blast radius. Every hit is logged.

**Prompt injection.** Untrusted external text flows into model prompts from
several tools: Tavily search results, GitHub `recent_changes`, Chrome history
titles, Strava activity names, and YouTube video titles/descriptions. Any of
these could contain text crafted to
steer the model. The blast radius is contained by design:

- **Chat** is the only place the model drives tool-calling freely, and every
  write tool there is confirmation-gated in code (`confirm_before` in
  `agent/loop.py`) — the model cannot send an email, create a calendar event,
  etc. without an explicit user "yes". The confirmation card also shows the
  substance of the pending action, including the email's *recipient* and a
  preview of its *body* (not just the subject), so an injected or hallucinated
  message can't be approved sight-unseen. The recipient isn't the model's to
  choose anyway: the model-facing `send_email` accepts only what its schema
  declares (subject, body) and pins the recipient to `BRIEF_TO_EMAIL`
  (`email.send_email_tool`), so an injected `to=` argument is dropped rather
  than silently honored — the loop's `fn(**fn_args)` dispatch would otherwise
  forward it. Chat card and background approval push render through the same
  describers (`toolset.describe_call`/`describe_call_detail`), so the two
  surfaces can't drift on what they disclose.
- **`morning_brief`, the daily learnings tasks, and `calendar_colorizer`** use the
  tool-free `complete_text` path — the model only writes narrative prose, it
  never calls a tool, so injected instructions have nothing to actuate.
  **`strava_download`** goes further and uses no model at all: it's a deterministic
  Python field-map from Strava activity to calendar event, so there's no prompt
  for injected activity text to hijack.
- All model output rendered to HTML is `html.escape`d and any URL is
  scheme-validated (`_safe_url`) before it reaches the page, so injected output
  can't smuggle scripts or `javascript:`/`data:` links into the dashboard.
- **Long-term memory** is a *persistence* vector: a fact saved from untrusted
  text (e.g. "remember what that page said") is injected into every future
  system prompt. It's rendered under a heading that frames saved items as
  reference facts to recall, *not* instructions to act on
  (`memory.render_memory_block()`), and capture is explicit (Craig-initiated,
  never a background scrape). `forget` is confirmation-gated so a poisoned
  memory can't be silently pruned to cover tracks.
- **Background tasks** (`run_in_background` → `bg_worker`) run detached, with no
  one present to confirm — so they follow an **"A + push-to-approve"** posture
  (`toolset.CONSEQUENTIAL_TOOLS`). The worker reads/researches/drafts freely, but
  any external/irreversible action (`send_email`, `send_morning_brief`) *pauses*
  the job and is pushed to Craig's phone for a
  tap-to-approve decision before it runs (the tap gets an immediate confirming
  push — "Approved" / "Denied" — since the ntfy buttons have no selected state
  of their own). So untrusted content pulled mid-task
  can never trigger an unattended irreversible action — the "no human in the
  loop" leg of the injection trifecta (untrusted input × consequential action ×
  no confirmation) is removed for exactly those actions. Going further, the
  tools that write **prompt-visible state** — `remember`/`pin`/`archive`/`forget`
  and `write_skill`/`delete_skill` — aren't in a background run's toolset at all
  (`toolset.UNATTENDED_EXCLUDED_TOOLS`): pinned memories and skills feed future
  system prompts, so a poisoned page read mid-job must not be able to plant a
  durable instruction that outlives the job, even behind an approval tap. The
  read side (`recall`, `read_skill`) stays available. Starting a background
  task is itself confirmation-gated in chat, so a job only runs because Craig
  said to.

The model can actuate a **consequential** write only with an explicit human
"yes": a tap in chat (`advance()`/`confirm_before`), or a phone approval for a
background task's external/irreversible action. The prose scheduled tasks keep
the model out of the write path entirely (`complete_text`; `strava_download`'s
deterministic Python field-map, which replaced an earlier `run_agent` path fed
by Strava activity *names*). The one place the model actuates a write
*unattended* is a background task performing a **reversible, internal** write to
Craig's own account (e.g. creating a task or recoloring an event) — a
deliberate, bounded exception, not a general autonomy grant. That line is two
editable sets in `toolset.py`: `CONSEQUENTIAL_TOOLS` (move a tool into it to
require approval, out of it to allow unattended) and `UNATTENDED_EXCLUDED_TOOLS`
(tools a background run doesn't get at all — the memory/skill writers, because
they feed future system prompts). If new work ever wires the model to a
write tool, hold this boundary — gate the consequential ones, or keep the model
out. See also the `web-content-untrusted-input` note in memory.

## What's NOT here

- No Anthropic/Claude API usage anywhere in this codebase (verified: no
  references to "anthropic" or "claude" in any `.py` file or `requirements.txt`).
  Claude Code was only used to *write* this code — it has no runtime role.
  Deleting Claude Code from this machine would not affect these tasks.
- `config/.env`, `config/google_credentials.json`, `config/google_token.json`,
  `config/github_starred_state.json`, and `logs/*.log` are gitignored — they
  contain secrets/tokens and machine-specific state.
