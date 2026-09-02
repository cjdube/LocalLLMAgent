# ScribeJay, from Wren's side

ScribeJay is the journaling agent. It was split out of Wren on 2026-08-26, after
a demo where the audience said out loud what the code already showed: a good part
of Wren was journaling, which is a different job from being an interactive agent.
On 2026-08-30 it moved out of this repo entirely, into a sibling checkout at
`~/Projects/ScribeJay`.

This page is what a Wren change needs to know. **How ScribeJay works is
ScribeJay's own `docs/architecture.md`** — do not describe its internals here, or
the two copies drift and the wrong one gets read.

## The seam

**ScribeJay writes the record, Wren reads it.**

Wren is the interactive agent: she reads the record — through the calendar and
the wiki — and takes action on request (book a meeting, set a reminder, send the
brief). ScribeJay never talks to anyone; it runs unattended under launchd and
leaves calendar events and vault pages behind.

That seam is why the raw-capture tools are not in Wren's registry. Asked "what
did I do yesterday?", Wren answers from `get_events_by_date` and `search_wiki`,
not by re-fetching Chrome history herself.

`tasks/daily_synthesis.py` is deliberately **not** journaling and stays with
Wren. Journaling is "write down what was done"; synthesis applies yesterday's
activity to notes and projects, which is reasoning.

## The direction of knowledge

**Wren knows about ScribeJay. ScribeJay does not know about Wren.**

That asymmetry is deliberate and it is the rule a change here can break:

- Wren names ScribeJay only where a row or a label has to say the word, and
  every one of those is display: `WREN_EXTERNAL_TASK_ROOTS` in `config/.env`;
  in `chat/insights.py`, the `local.scribejay.` branch of `_agent_of`, the
  `scribejay-` routine rows, the `_SOURCE_TITLES` spelling fix (so a row reads
  "ScribeJay" and not "Scribejay"), and `SCRIBEJAY_CONFIG`
  (`~/.scribejay/config.json`), which it reads one key out of for the `/map`
  agent's model label; a log-grouping comment in `chat/logview.py`; and the
  agent's colour and icon assets under `chat/static/` and `chat/views/map.html`.
  Not one is an import or a shell-out. The config read is a plain file read,
  like the plists and logs — that is the whole extent of the coupling.
- ScribeJay names Wren nowhere. It has its own `.env`, its own venv, its own
  Google token, its own `SCRIBEJAY_*` backend chain, and its own copy of the
  launchd healer. Nothing it does depends on this repo existing.

So a Wren change may read ScribeJay's *output* (vault pages, calendar events,
log files). It must never import from the checkout, shell into it, or write to
its config. If Wren needs something ScribeJay has, the answer is a second copy
here, not a reach across.

## What left Wren's registry when the split happened

Removed from `agent/toolset.py` entirely — `TOOLS`, `DISPATCH`, the gating sets
and the group tables:

- `fetch_strava`, `fetch_chrome_history`, `fetch_liked_videos` — the whole
  `activity` tool group, and its `GROUP_KEYWORDS` entry.
- `recolor_event` — coloring the past is journaling.

Wren went from 55 registered tools to 51 at the time of the split. The registry
has grown since — `len(TOOLS)` is 57 today — so read that as what the split
removed, not as a current count.

Two capture modules **stay** in `agent/tools/`: `tasks/daily_synthesis.py` calls
`fetch_chrome_history` and `fetch_liked_videos`, and synthesis is Wren's. They
are plain library modules with no `TOOL_SCHEMA`, and `tests/test_toolset.py` has
two guard tests that fail if one grows a schema back or reappears in the
registry. ScribeJay carries its own copies; that duplication is the intended
cost of the boundary above.

`agent/tools/strava.py` had no such second caller and left with ScribeJay, as did
`set_event_color()` from `agent/tools/calendar.py`.

## How ScribeJay reaches this dashboard

Through `WREN_EXTERNAL_TASK_ROOTS`, the same federation the wiki agent uses —
[external-tasks.md](external-tasks.md) has the mechanism and the three log-format
rules an external task has to honor.

Two consequences worth knowing before you debug a missing row:

- **Run history comes from ScribeJay's `logs/`, not Wren's.** The dashboard reads
  `<root>/logs/<key>.log` and parses `Starting … run` / `… run complete` lines.
  A ScribeJay log file in Wren's `logs/` is a leftover, not a source.
- **Its keys carry the `scribejay-` prefix**, from the short name in the env
  variable. `ROUTINE_USES` / `ROUTINE_WRITES` in `chat/insights.py` are keyed on
  the prefixed form; drop the prefix and the row loses its labels silently.

`chat/insights.py:_agent_of` checks the `local.scribejay.` label **before** the
`external` flag, so the eight routines stay in their own bucket instead of the
grey external one. `tests/test_insights.py` pins both halves.

## Running one by hand

From ScribeJay's checkout, with its own interpreter:

```bash
cd ~/Projects/ScribeJay && .venv/bin/python -m scribejay.daily_chrome_learnings
```

Wren's `.venv/bin/python -m scribejay.…` does not work and is not meant to.
