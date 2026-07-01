# LocalLLMAgent

A fully local, unattended agent system that runs on this Mac Mini. It performs
scheduled tasks (Strava-to-calendar logging, a morning brief email, a weekly
retrospective doc entry) using a **local LLM served by Ollama** — no Anthropic/
Claude API calls at runtime, and no Claude Code/Cowork-managed scheduling.
Scheduling is handled entirely by macOS `launchd`.

## Architecture

```
launchd (per-task .plist, timed)
   -> python -m tasks.<task_name>
       -> fetches data via tool modules (weather, calendar, Strava, Chrome history)
       -> calls the local model via Ollama's HTTP API for anything that
          needs natural-language composition or tool-calling
       -> writes the result out (email / calendar event / Google Doc)
       -> logs everything to logs/<task_name>.log
```

**The model is swappable.** Nothing in this codebase is Gemma-specific — the
model name is just `OLLAMA_MODEL` in `config/.env`. Swap models with
`ollama pull <model>` + edit that one line. The one thing to verify after a
swap: `daily_log.py` relies on Ollama's tool-calling protocol (`tools` /
`tool_calls`), so a model with weak tool-calling support may not drive it
reliably — the other two tasks only need plain text completion.

### `agent/loop.py` — the two ways tasks talk to the model

- **`run_agent(...)`** — the full tool-calling loop. Sends messages + tool
  schemas to Ollama's `/api/chat`, dispatches any `tool_calls` to local Python
  functions, feeds results back, repeats (capped at 6 iterations) until the
  model returns a final text answer. Used by `daily_log.py`.
- **`complete_text(...)`** — a single-turn, tool-free completion. Used when
  the surrounding structure (HTML layout, doc template) is built
  deterministically in Python and the model is only asked to write a
  paragraph of prose to slot in. Used by `morning_brief.py` and
  `weekly_learnings.py` — this is more reliable than trusting a small local
  model to produce well-formed HTML/markdown on its own.

### `agent/tools/` — one module per capability

| Module | Purpose |
|---|---|
| `weather.py` | OpenWeatherMap forecast for `DEFAULT_LOCATION` |
| `strava.py` | Strava activities via Composio, for a given date |
| `calendar.py` | Google Calendar read/write (`get_upcoming_events`, `get_events_in_range`, `log_calendar_event` — idempotent via `source_id`) |
| `email.py` | Send email via Gmail API (plain text or HTML) |
| `docs.py` | Read/write the Weekly Learning & Project Log Google Doc |
| `chrome_history.py` | Read Chrome's local history DB for a date range |
| `google_auth.py` | Shared OAuth helper — one cached token for Calendar, Gmail, and Docs scopes |

Every tool module is runnable standalone for testing, e.g.:
```bash
.venv/bin/python -m agent.tools.weather --location "Boston,MA,US"
.venv/bin/python -m agent.tools.calendar list --hours-ahead 48
```

## Scheduled tasks

| Task | Schedule | What it does |
|---|---|---|
| `tasks/morning_brief.py` | Daily 6:00 AM | Fetches weather + next-24h calendar events, has the model write a short "at a glance" summary, assembles a styled HTML email, sends it via Gmail. |
| `tasks/daily_log.py` | Daily 6:15 AM | Fetches yesterday's Strava activities, has the model (via `run_agent`, tool-calling) log each one to Google Calendar. Deduped by `source_id` (Strava activity id) so re-runs never create duplicates. |
| `tasks/weekly_learnings.py` | Mondays 5:00 AM | Computes the most recently completed Mon–Sun week, pulls calendar events (categorized by color) + Chrome browsing history + the previous doc entry (for carry-forwards), has the model draft a 4-section retrospective, writes it to the Weekly Learning & Project Log doc. If the doc write fails, emails the draft instead so it's never silently lost. |
| `tasks/calendar_colorizer.py` | Daily 5:00 PM | Fetches yesterday's calendar events, has the model guess a category per event title (Work/LLC, AARP, Fitness, Meal Prep, Domestic/Chores, Meetings, Travel, Appointments, or Uncategorized) and returns a colorId per event, then patches each event's color. Always re-classifies, even events colored by a previous run or by hand. On failure, emails a notice. |

All four are **fully unattended** — no prompts, no approval steps. This is a
deliberate difference from the original interactive Claude Code skills these
were ported from (`ai-memory` / `AgentOS`), which asked clarifying questions
and waited for approval before writing anywhere. There's nobody to ask at
5am, so these versions infer everything from the data and just act.

## Scheduling — launchd

Each task has a `.plist` in `launchd/`, copied to `~/Library/LaunchAgents/`
and loaded with `launchctl load`. `launchd` was chosen over `cron` because it
survives sleep/wake and is the native macOS mechanism — no Claude Code/Cowork
involvement in scheduling at all.

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
2. Create the venv (needs Python 3.10+, composio requires it):
   ```bash
   python3.12 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
3. Copy `config/.env.example` to `config/.env` and fill in:
   - `OPENWEATHERMAP_API_KEY` — [openweathermap.org](https://openweathermap.org/api)
   - `COMPOSIO_API_KEY`, `STRAVA_USER_ID`, `STRAVA_CONNECTED_ACCOUNT_ID` — from your Composio Strava connection
   - `WEEKLY_LOG_DOC_ID` — the Google Doc ID (from its URL) for the Weekly Learning & Project Log
   - `BRIEF_TO_EMAIL`, `DEFAULT_LOCATION` — your own values
4. Google OAuth setup (Calendar + Gmail + Docs):
   - [console.cloud.google.com](https://console.cloud.google.com/) → create/select a project
   - Enable the **Google Calendar API**, **Gmail API**, and **Google Docs API**
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
6. Load the launchd plists (see Scheduling section above).

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

## What's NOT here

- No Anthropic/Claude API usage anywhere in this codebase (verified: no
  references to "anthropic" or "claude" in any `.py` file or `requirements.txt`).
  Claude Code was only used to *write* this code — it has no runtime role.
  Deleting Claude Code from this machine would not affect these tasks.
- `config/.env`, `config/google_credentials.json`, `config/google_token.json`,
  and `logs/*.log` are gitignored — they contain secrets/tokens and
  machine-specific state.
