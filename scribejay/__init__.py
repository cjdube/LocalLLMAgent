"""ScribeJay — the journaling agent.

ScribeJay keeps the record of what actually happened. Strava activities logged onto
the calendar, yesterday's events colour-coded after the fact, Claude Code working
time turned into time blocks, and a daily page in the Obsidian vault built from
Chrome history, YouTube Likes, AI chats and the commits he made.

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
    agent.tools.{calendar, email, chrome_history, youtube, strava, gmail_read,
                 clickup}
    tasks._common         -- setup_logger / notify_failure
    tasks._urls           -- safe_url

Those pull in agent.dates, agent.backends, agent.tools.{notify, push_log,
google_auth, learnings_file, _http} transitively, which is why the extraction
estimate in docs/scribejay.md is larger than this list.

gmail_read was added to the porch on 2026-08-26 for daily_correspondence, and only
for `fetch_sent_metadata` and `my_address` — both library functions, neither
model-facing. It is the same arrangement agent.tools.calendar already has, where
`get_events_in_range` and `set_event_color` back the colorizer without being
registered tools. ScribeJay must not reach for the model-facing halves of that
module (`search_mail`, `read_email`): reading a mailbox on request is Wren's job.

clickup was added on 2026-08-27 for daily_commits, and only for `closed_tasks` —
again a library function with no TOOL_SCHEMA, the same arrangement as the two
above. The alternative on the table was giving ScribeJay its own small ClickUp
fetch, and it was rejected: duplicating an HTTP layer, its paging, its timeouts
and its timezone conversion is exactly what the porch exists to avoid. ScribeJay
must not reach for the six registered tools in that module — reading or writing a
backlog on request is Wren's job. What ScribeJay wants is narrower than any of
them anyway: what reached Done yesterday, which nothing asks a model about.

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
