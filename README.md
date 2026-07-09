# LocalLLMAgent

A fully local agent system that runs on this Mac Mini, powered by a **local LLM
served by Ollama** — no Anthropic/Claude API calls at runtime, and no Claude
Code/Cowork-managed scheduling. The agent has a name: **Wren**. It works two
ways:

- **Scheduled tasks** (Strava-to-calendar logging, a morning brief email, a
  weekly retrospective doc entry) — unattended, triggered entirely by macOS
  `launchd`, no human in the loop.
- **Ad hoc chat** — a small always-on web app (`chat/server.py`) so Craig can
  talk to Wren directly and have her take action on request, from anywhere,
  over Tailscale. See "Wren — ad hoc chat" below.

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
swap: `daily_log.py` relies on Ollama's tool-calling protocol (`tools` /
`tool_calls`), so a model with weak tool-calling support may not drive it
reliably — the other two tasks only need plain text completion.

### `agent/loop.py` — how tasks and chat talk to the model

- **`run_agent(...)`** — the full tool-calling loop for unattended tasks. Sends
  messages + tool schemas to Ollama's `/api/chat`, dispatches any `tool_calls`
  to local Python functions immediately, feeds results back, repeats (capped
  at `MAX_TOOL_ITERATIONS`) until the model returns a final text answer. Used by
  `daily_log.py`. Internally a thin wrapper over `advance()` (below).
- **`complete_text(...)`** — a single-turn, tool-free completion. Used when
  the surrounding structure (HTML layout, doc template) is built
  deterministically in Python and the model is only asked to write a
  paragraph of prose to slot in. Used by `morning_brief.py` and
  `weekly_learnings.py` — this is more reliable than trusting a small local
  model to produce well-formed HTML/markdown on its own.
- **`advance(messages, tools, dispatch, confirm_before=...)`** /
  **`resolve(messages, call, approved, dispatch)`** — the lower-level,
  resumable primitive `chat/server.py` uses for ad hoc chat. `advance()` auto-
  executes any tool call whose name isn't in `confirm_before`, same as
  `run_agent`; the moment one *is* in that set, it stops and returns the
  pending call without executing it, so a web request can show the user what's
  about to happen and come back later (a second HTTP request calling
  `resolve()` then `advance()` again) to actually run it. `run_agent` calls
  `advance()` with an empty `confirm_before`, so nothing ever pauses —
  identical behavior to before this was extracted.

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
| `github_starred.py` | List starred GitHub repos, optionally filtered to those pushed to since a given timestamp, with a `recent_changes` summary (release notes or recent commit subjects) per matched repo |
| `strava.py` | Strava activities via the Strava API (own OAuth app), for a given date. Run `--authorize` once to mint a refresh token |
| `calendar.py` | Google Calendar read/write (`get_upcoming_events`, `get_events_in_range`, `log_calendar_event` — idempotent via `source_id`) |
| `email.py` | Send email via Gmail API (plain text or HTML) |
| `learnings_file.py` | Write the weekly review to a Markdown file in the Obsidian vault (`LEARNINGS_DIR`) and read the most recent one back for carry-forward |
| `wiki.py` | Read-only search of the learnings wiki (`WIKI_VAULT_PATH`) so Wren can answer "what did I decide about X" — `read_wiki_index`, `list_wiki_pages`, `read_wiki_page` over the concept pages built by ObsidianWikiAgent, plus `list_weekly_reviews`/`read_weekly_review` over the raw weeks. Requires the vault drive mounted |
| `google_tasks.py` | Google Tasks read/write (`get_tasks`, `get_tasks_due_soon`, `create_task`, `update_task_due_date`, `complete_task`) |
| `chrome_history.py` | Read Chrome's local history DB for a date range |
| `memory.py` | Persistent long-term memory in two tiers — `remember` (archival, search-only), `pin` (active, injected into every system prompt), `recall` (search either tier, optionally by category), `archive` (demote active→archival), `forget` (delete). Stored in `config/wren_memory.json`; archival facts track an `access_count` |
| `google_auth.py` | Shared OAuth helper — one cached token for Calendar, Gmail, and Tasks scopes |

Every tool module is runnable standalone for testing, e.g.:
```bash
.venv/bin/python -m agent.tools.weather --location "Boston,MA,US"
.venv/bin/python -m agent.tools.calendar list --hours-ahead 48
```

## Scheduled tasks

| Task | Schedule | What it does |
|---|---|---|
| `tasks/morning_brief.py` | Daily 6:00 AM | Fetches weather + next-24h calendar events + Google Tasks past due or due within 48h (via `google_tasks.get_tasks_due_soon`) + starred GitHub repos pushed to since the last brief (via `github_starred.fetch_starred_repos`, cursor persisted in `config/github_starred_state.json`), has the model write a short "at a glance" summary and a one-sentence intro for the starred-repos section, assembles a styled HTML email (weather / calendar / tasks due soon / Starred Repos sections), sends it via Gmail. The pipeline lives in `build_and_send_brief()`, shared with the chat `send_morning_brief` tool. |
| `tasks/daily_log.py` | Daily 6:15 AM | Fetches yesterday's Strava activities, has the model (via `run_agent`, tool-calling) log each one to Google Calendar. Deduped by `source_id` (Strava activity id) so re-runs never create duplicates. |
| `tasks/weekly_learnings.py` | Mondays 5:00 AM | Computes the most recently completed Mon–Sun week, pulls calendar events (categorized by color) + Chrome browsing history + the previous week's file (for carry-forwards), has the model draft a 4-section retrospective, writes it as `Strategic-Weekly-Review-<week-ending>.md` in `LEARNINGS_DIR` (Obsidian vault, one file per week). If the write fails — e.g. the external drive isn't mounted — it emails the draft instead so it's never silently lost. |
| `tasks/calendar_colorizer.py` | Daily 5:00 PM | Fetches yesterday's calendar events, has the model guess a category per event title (Work/LLC, AARP, Fitness, Meal Prep, Domestic/Chores, Meetings, Travel, Appointments, or Uncategorized) and returns a colorId per event, then patches each event's color. Always re-classifies, even events colored by a previous run or by hand. On failure, emails a notice. |

All four are **fully unattended** — no prompts, no approval steps. This is a
deliberate difference from the original interactive Claude Code skills these
were ported from (`ai-memory` / `AgentOS`), which asked clarifying questions
and waited for approval before writing anywhere. There's nobody to ask at
5am, so these versions infer everything from the data and just act.

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
  its latest release notes or recent commit subjects) — all read-only,
  execute immediately — plus `log_calendar_event`,
  `send_email`, `recolor_event`, and `send_morning_brief` (state-changing —
  see confirmation gate below).
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
  active fact back to search-only. All execute immediately except `forget` (delete
  by id), which is confirmation-gated. See the memory tool in the tools table
  above. Wren can also search Craig's **learnings wiki** (`WIKI_VAULT_PATH`, the
  Obsidian vault ObsidianWikiAgent maintains): `read_wiki_index`,
  `list_wiki_pages`, `read_wiki_page` navigate the concept pages the way that
  project's own query CLI does — read the index, open the relevant page(s),
  answer and cite them — and `list_weekly_reviews`/`read_weekly_review` fall
  back to the raw weekly reviews for a week not yet summarized into the wiki. All
  read-only; they return an error (rather than raise) if the vault drive isn't
  mounted. Not yet wired up for chat:
  reading/writing the Weekly Log doc — those functions don't have a
  `TOOL_SCHEMA` yet (only ever called directly from Python by
  `weekly_learnings.py`). Same pattern extends it later if wanted.
- **Confirmation gate:** before `log_calendar_event`, `send_email`,
  `recolor_event`, `send_morning_brief`, or `forget` actually runs, the chat UI shows
  what Wren wants to do and waits for a tap to confirm or cancel — enforced in
  code (`advance()`'s `confirm_before` set in `agent/loop.py`), not just
  requested in the prompt, so it doesn't depend on the small local model
  reliably remembering to ask.
- **Session memory:** in-memory only, per browser session (a signed cookie
  carries a session id; conversation history lives in a server-side dict
  keyed by it). Fresh conversation on first visit or after tapping "New
  chat"; lost entirely on server restart — no persistence across sessions.
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
see what's scheduled, whether it's working, what Wren can do, and to talk to
her. It reuses the chat server's token auth, so no separate login.

```
chat/
  insights.py         # read/parse layer: plists -> schedules, logs -> runs,
                      # tool schemas -> capabilities, plus the run-now manager
  static/dashboard.html  # single-page dashboard UI (vanilla JS, no build step)
  static/favicon.svg  # Wren mark (cream wren on terracotta) — header logo + favicon
  static/{favicon-32,apple-touch-icon,icon-512}.png  # raster fallbacks
```

It's a single page (no tabs) — a scrollable left column with the chat docked
on the right so you can talk to Wren while watching a run:

- **Capabilities** (top of the left column) — auto-generated from the chat
  tools' `TOOL_SCHEMA`s as chips, grouped read-only (Wren runs them itself) vs.
  the confirmation-gated write tools. Click a chip to see its parameters. Always
  in sync with what's actually registered — no separate docs to maintain.
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
- **Chat dock** — the same chat UI as `/`, always visible on the right, using
  the existing `/chat` endpoints (confirmation prompts appear inline in the dock).

**Run now runs the real task, side effects and all** — `morning_brief` sends the
actual email, `daily_log`/`calendar_colorizer` write real calendar events —
exactly what the schedule does, just now. The button asks for a click-through
confirm and refuses a second concurrent run of the same task. It's a read-only
window otherwise: schedules are still edited by hand in the `.plist` files
(see below), not from the UI.

New dashboard routes on the chat server, all behind the same auth:
`GET /dashboard`, `GET /api/schedules`, `GET /api/runs/<task>`,
`GET /api/runs/<task>/<run_id>`, `GET /api/capabilities`,
`POST /api/run/<task>`, `GET /api/run/<task>/status`.

### Memories

`http://127.0.0.1:8420/memories` is a read-only view of `memory.py`'s store
(same auth as the dashboard): an **active** table (pinned facts always in the
system prompt) and an **archival** table (search-only facts), each with
category and created date. Archival is sorted by `access_count` descending —
the facts that actually get recalled float to the top. Managing memories
(pin/archive/forget) is still chat-only; this page is just for seeing what's
there. Backed by `GET /api/memories`, which calls `memory.recall()` with no
query so viewing the page never bumps any access counts.

## Scheduling — launchd

Each task (and the always-on chat server) has a `.plist` in `launchd/`,
copied to `~/Library/LaunchAgents/` and loaded with `launchctl load`.
`launchd` was chosen over `cron` because it survives sleep/wake and is the
native macOS mechanism — no Claude Code/Cowork involvement in scheduling at
all. The four scheduled tasks use `StartCalendarInterval`; Wren's chat server
(`com.craigdube.localllmagent.wren.plist`) uses `RunAtLoad` + `KeepAlive`
instead, since it needs to just stay running rather than fire on a timer.

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
   - `LEARNINGS_DIR` — directory the weekly review Markdown files are written to
     (defaults to `/Volumes/T7/Obsidian/learnings/raw`)
   - `WIKI_VAULT_PATH` — root of the Obsidian vault Wren reads to answer "what did
     I decide about X" via the `wiki.py` tools (defaults to
     `/Volumes/T7/Obsidian/learnings`; the wiki itself is built by ObsidianWikiAgent)
   - `GOOGLE_TASKLIST_ID` — optional. By default `get_tasks`/`get_tasks_due_soon`
     read across every Google Tasks list on the account (tasks are commonly split
     across several named lists); set this to a specific list id to scope reads
     — and the default list new tasks land in — to just that one list
   - `BRIEF_TO_EMAIL`, `DEFAULT_LOCATION` — your own values
   - `WREN_CHAT_TOKEN`, `FLASK_SECRET_KEY` — generate each with
     `python -c "import secrets; print(secrets.token_hex(32))"`; leave
     `WREN_CHAT_PORT` at its default unless it conflicts with something else
4. Google OAuth setup (Calendar + Gmail + Tasks):
   - [console.cloud.google.com](https://console.cloud.google.com/) → create/select a project
   - Enable the **Google Calendar API**, **Gmail API**, and **Google Tasks API**
   - OAuth consent screen → External → add yourself as a test user
   - Credentials → Create Credentials → OAuth client ID → **Desktop app**
   - Download the JSON as `config/google_credentials.json`
   - Run `.venv/bin/python -m agent.tools.google_auth` — opens a browser once, caches `config/google_token.json`
5. Test each task manually before trusting the schedule:
   ```bash
   .venv/bin/python -m tasks.morning_brief
   .venv/bin/python -m tasks.daily_log
   .venv/bin/python -m tasks.weekly_learnings
   .venv/bin/python -m tasks.calendar_colorizer
   ```
6. Test the chat server manually: `.venv/bin/python -m chat.server`, then
   visit `http://localhost:8420` and log in with `WREN_CHAT_TOKEN`. (Fine for
   a quick smoke test since the session cookie is `Secure` — a real browser
   may not persist login over plain `http://`; use the HTTPS tailnet URL from
   step 8 for anything beyond a one-off check.)
7. Load the launchd plists (see Scheduling section above) — this now
   includes `com.craigdube.localllmagent.wren.plist`, the always-on chat
   server.
8. Install [Tailscale](https://tailscale.com) on the Mac Mini and your phone
   if you want to reach Wren's chat from outside your home WiFi, then run
   `tailscale serve --bg 8420` (see "Wren — ad hoc chat" above for the
   one-time HTTPS Certificates setup this needs).

## Adding a new scheduled skill

1. Add any new tool module(s) to `agent/tools/` (a `TOOL_SCHEMA` dict + a
   plain callable function, following the existing modules as a template).
2. Write `tasks/<name>.py` — decide upfront whether it needs `run_agent`
   (multi-step tool-calling) or `complete_text` (deterministic Python
   structure + one narrative paragraph from the model). Prefer `complete_text`
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

The tests are pure/offline — they mock the model and network, so no Ollama or
API keys are needed. They cover the date resolver, run-history parsing and
caching, the login throttle, the morning-brief rendering helpers, and the chat
server's auth + confirm/rollback flow. After editing the always-on chat server,
restart it (it runs under launchd) so the changes take effect.

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

**Prompt injection.** Untrusted external text flows into model prompts from
several tools: Tavily search results, GitHub `recent_changes`, Chrome history
titles, and Strava activity names. Any of these could contain text crafted to
steer the model. The blast radius is contained by design:

- **Chat** is the only place the model drives tool-calling freely, and every
  write tool there is confirmation-gated in code (`confirm_before` in
  `agent/loop.py`) — the model cannot send an email, create a calendar event,
  etc. without an explicit user "yes". The confirmation card also shows the
  substance of the pending action, including a preview of the email *body*
  (not just the subject), so an injected or hallucinated message can't be
  approved sight-unseen.
- **`morning_brief`, `weekly_learnings`, and `calendar_colorizer`** use the
  tool-free `complete_text` path — the model only writes narrative prose, it
  never calls a tool, so injected instructions have nothing to actuate.
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

The one place model-driven tool-calling meets an **un-gated** write is
`tasks/daily_log.py`: it runs `run_agent` and can call `log_calendar_event`,
fed by Strava activity *names*. This is an accepted risk — an attacker would
need to control the user's own Strava account to influence it, and the only
action is creating a calendar event (idempotent via `source_id`). If the trust
assumptions here ever change (e.g. a shared Strava, or new un-gated write tools
reachable from a scheduled task), revisit this boundary. See also the
`web-content-untrusted-input` note in memory.

## What's NOT here

- No Anthropic/Claude API usage anywhere in this codebase (verified: no
  references to "anthropic" or "claude" in any `.py` file or `requirements.txt`).
  Claude Code was only used to *write* this code — it has no runtime role.
  Deleting Claude Code from this machine would not affect these tasks.
- `config/.env`, `config/google_credentials.json`, `config/google_token.json`,
  `config/github_starred_state.json`, and `logs/*.log` are gitignored — they
  contain secrets/tokens and machine-specific state.
