"""ScribeJay — the journaling agent.

ScribeJay keeps the record of what actually happened. Strava activities logged onto
the calendar, yesterday's events colour-coded after the fact, Claude Code working
time turned into time blocks, and a daily page in the Obsidian vault built from
Chrome history, YouTube Likes and AI chats.

Wren (agent/, chat/, tasks/) is the interactive agent: she READS the record ScribeJay
writes — through the calendar and the wiki — and acts on request. That is the
seam. **ScribeJay writes the record, Wren reads it.**

What is NOT journaling and stays with Wren: tasks/daily_synthesis.py. Journaling
is "write down what was done"; synthesis applies yesterday's activity to notes and
projects, which is reasoning.

## Shape

ScribeJay is a PIPELINE agent, not a tool-calling one: gather -> one
`complete_text()` call -> write. It has no tool registry and never calls
`agent.loop.advance()`. Keep it that way — the whole point of the split is that
Wren's tool budget is not spent on capture.

## Import rules (the porch)

ScribeJay may import ONLY these from the rest of the repo:

    agent.prefs, agent.store, agent.activity_log
    agent.loop            -- complete_text and warm_model only, never advance()
    agent.tools.{calendar, email, chrome_history, youtube, strava}
    tasks._common         -- setup_logger / notify_failure
    tasks._urls           -- safe_url

Those pull in agent.dates, agent.backends, agent.tools.{notify, push_log,
google_auth, learnings_file, _http} transitively, which is why the extraction
estimate in docs/scribejay.md is larger than this list.

ScribeJay must NOT import `agent.toolset` or anything under `chat/`. Nothing under
`agent/`, `chat/` or `tasks/` may import `scribejay.*`. `evals/` is the one
exception — it is neither agent, and it already reaches into both.

That list is the porch: adding to it is a deliberate decision, not a drive-by
import, because the porch is exactly what would have to travel with ScribeJay if it
is ever extracted into its own public repo. See docs/scribejay.md.

## Model

ScribeJay resolves its own backend (scribejay/model.py) — `SCRIBEJAY_LLM_BACKEND` and the
per-task overrides, independent of Wren's `WREN_*` variables. Local Ollama by
default; a future OpenRouter backend is one environment variable away.
"""
