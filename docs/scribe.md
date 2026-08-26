# Scribe — the journaling agent

Scribe keeps the record of what actually happened. It was split out of Wren on
2026-08-26, after a demo where the audience said out loud what the code already
showed: a good part of Wren was journaling, which is a different job from being
an interactive agent.

Code: `scribe/`. Charter: the `scribe/__init__.py` docstring — read it before
adding anything here.

## The seam

**Scribe writes the record, Wren reads it.**

Wren is the interactive agent: she reads the record — through the calendar and
the wiki — and takes action on request (book a meeting, set a reminder, send the
brief). Scribe never talks to anyone; it runs unattended under launchd and
leaves calendar events and vault pages behind.

That seam is why the raw-capture tools left Wren's registry (below). Asked "what
did I do yesterday?", Wren answers from `get_events_by_date` and `search_wiki`,
not by re-fetching Chrome history herself.

`tasks/daily_synthesis.py` is deliberately **not** journaling and stays with
Wren. Journaling is "write down what was done"; synthesis applies yesterday's
activity to notes and projects, which is reasoning.

## Shape: a pipeline agent, not a tool-calling one

Every Scribe task is the same three steps:

```
gather (deterministic Python)  ->  one complete_text() call  ->  write
```

There is no tool registry and no call to `agent.loop.advance()`. The model
writes blurbs and scores; Python owns dates, structure, URLs and file assembly,
per the repo's small-local-model rules. Keep it that way — the point of the
split is that Wren's tool budget is not spent on capture.

## What moved

| Task | Schedule | launchd label |
|---|---|---|
| `scribe/ai_chat_learnings.py` | Daily 4:30 AM | `local.scribe.aichatlearnings` |
| `scribe/claude_time_blocks.py` | Daily 4:45 AM | `local.scribe.claudetimeblocks` |
| `scribe/daily_youtube_learnings.py` | Daily 5:05 AM | `local.scribe.dailyyoutubelearnings` |
| `scribe/daily_chrome_learnings.py` | Daily 5:15 AM | `local.scribe.dailychromelearnings` |
| `scribe/strava_download.py` | Daily 5:50 AM | `local.scribe.stravadownload` |
| `scribe/calendar_colorizer.py` | Daily 5:00 PM | `local.scribe.calendarcolorizer` |

Plus two helpers: `scribe/journal.py` (the video-list section and the
"is this draft substantive?" check) and `scribe/transcripts.py` (Claude Code and
Gemini transcript readers, shared by two tasks).

**The module basenames did not change, on purpose.** `chat/insights.py` derives
a task's key from its plist's `StandardOutPath` basename, not from the label, so
keeping `logs/strava_download.log` et al. identical meant every dashboard row
kept its full run history through the move. The plist labels changed; the log
paths did not.

## What left Wren's registry

Removed from `agent/toolset.py` entirely — `TOOLS`, `DISPATCH`, the gating sets
and the group tables:

- `fetch_strava`, `fetch_chrome_history`, `fetch_liked_videos` — the whole
  `activity` tool group, and its `GROUP_KEYWORDS` entry.
- `recolor_event` — coloring the past is journaling.

Wren went from 55 registered tools to 51.

The three capture modules **stay** in `agent/tools/` — `tasks/daily_synthesis.py`
calls `fetch_chrome_history` and `fetch_liked_videos`, and synthesis is Wren's.
Moving them into `scribe/` would make Wren import Scribe, which is backwards.
They are now plain library modules with no `TOOL_SCHEMA`;
`tests/test_toolset.py` has two guard tests that fail if one grows a schema back
or if a capture tool reappears in the registry.

`agent/tools/calendar.py` keeps `set_event_color()` as a plain function for
`calendar_colorizer` — it is just no longer model-facing.

## The porch

Scribe may import only this list from the rest of the repo:

```
agent.prefs, agent.store, agent.activity_log
agent.loop            -- complete_text and warm_model only, never advance()
agent.tools.{calendar, email, chrome_history, youtube, strava}
tasks._common         -- setup_logger / notify_failure
tasks._urls           -- safe_url
```

Scribe must **not** import `agent.toolset` or anything under `chat/`. Nothing
under `agent/`, `chat/` or `tasks/` may import `scribe.*`. `evals/` is the one
exception — it is neither agent, and it already reaches into both.

That list is the porch. Adding to it is a deliberate decision, not a drive-by
import, because the porch is exactly what would have to travel with Scribe if it
is ever extracted into its own repo. Transitively it pulls in `agent.dates`,
`agent.backends`, and `agent.tools.{notify, push_log, google_auth,
learnings_file, _http}` — roughly 3,900 lines, which is why "just copy it" is
not the cheap answer.

`agent/activity_log.py` is on the porch because **both** agents use it:
`prior_day`, the exclusion helpers and the compaction caps are shared by Scribe's
daily tasks and by `daily_synthesis`. It lives under `agent/` for that reason,
not by accident.

## Model backend

Scribe resolves its own backend in `scribe/model.py`:

```
SCRIBE_<TASK_KEY>_BACKEND  ->  SCRIBE_LLM_BACKEND  ->  ollama
```

Task keys match the `scribe/` module names. There is deliberately **no** fallback
to the `WREN_*` variables — see the `scribe/model.py` docstring. Every run logs
which backend it resolved to and where that came from, because the failure mode
is silent: an unset variable is not an error, just a smaller model and a thinner
draft.

**Migration trap.** `SCRIBE_DAILY_CHROME_LEARNINGS_BACKEND=gemini` and
`SCRIBE_DAILY_YOUTUBE_LEARNINGS_BACKEND=gemini` are required, not optional, if
those tasks were on the cloud before. `agent/activity_log.py` sizes
`MAX_CHROME_SITES=40` / `MAX_PAGES_PER_SITE=6` for a cloud model; routed back to
Ollama, sections vanish from the draft instead of erroring. After any change
here, run the task for real and read the log — the `backend:` line, then whether
both `### Tools & Tech Encountered` and `### Product & Strategy` are filled in.

A future OpenRouter backend is one `.env` line and no code change in `scribe/`.

## Running one by hand

```bash
.venv/bin/python -m scribe.claude_time_blocks --dry-run
```

```bash
.venv/bin/python -m scribe.daily_chrome_learnings
```

## If Scribe ever becomes its own public repo

Not abandoned — sequenced. The expensive part of extracting Scribe is not moving
the task files, it is untangling what they import, and the porch rule above is
that untangling. Once it has run in this shape for a few quiet weeks:

1. `git filter-repo` `scribe/` plus the porch modules into a new repo.
2. Publish the porch as a small installable package both repos `pip install`, so
   the next shared feature (OpenRouter) is written once, not twice. Copying the
   ~3,900 lines instead guarantees drift.
3. Point Wren's `WREN_EXTERNAL_TASK_ROOTS` at the new checkout so the dashboard
   keeps showing Scribe's runs — the ObsidianWikiAgent pattern, already solved
   in [external-tasks.md](external-tasks.md). Scribe already honors the three
   log-format rules that doc lists, because it inherits `setup_logger`.

New costs to accept then: a second Google consent (or one token path shared by
env), a second venv and `.env`, and Wren no longer seeing Scribe's failure
pushes unless both point at one `push_log.json`.
