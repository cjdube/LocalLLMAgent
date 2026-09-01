# Model usage ledger

Every model call any agent makes appends one JSON line to that agent's own
`logs/usage.jsonl`. The `/activity` page reads all of them.

The numbers were always there — `ollama_chat model=... prompt_tokens=...` has
been in `wren.log` from the start — but as loose text in a file that rotates at
8MB. Nothing could count them, nothing survived, and the dollar cost of a Claude
Code build was printed into a report and dropped. The ledger is the same numbers
in a shape that can be summed.

## The record

One object per line, appended, never rewritten in place:

| Field | What it holds |
| --- | --- |
| `ts` | Local ISO-8601 to the second. Naive local, same as the run logs. |
| `agent` | `wren`, `scribejay`, `wiki`. Which codebase made the call. |
| `task` | The logger name, which `setup_logger` already sets to the task: `morning_brief`, `daily_synthesis`, `wren` for the chat server. |
| `caller` | `advance` (a tool-calling turn), `complete_text` (a one-shot), `advance_out_of_steps`. Separates a chat turn from a scheduled job inside one task name. |
| `backend` | `ollama`, `gemini`, `anthropic`. |
| `model` | The model string the backend actually used. |
| `prompt_tokens`, `output_tokens`, `thinking_tokens` | Counts as reported. Any of them may be null — Ollama does not report thinking, and a failed call reports nothing. |
| `num_ctx` | The context window the call was given (Ollama only). |
| `duration_ms` | Wall clock around the call, measured by us, not by the backend. |
| `finish_reason` | `stop`, `length` (Ollama's word for "hit the cap"), `MAX_TOKENS` (Gemini's), `cancelled`. |
| `tools_offered` | How many tools were in the request. A large prompt with 0 tools is a template fill; with 20 it is a chat turn. |
| `ok`, `error` | `false` plus a reason when the call raised. |
| `cost_usd` | Dollars. `0.0` for local. Estimated for Gemini. Reported for Claude Code. **`null` when the model is not in the price table** — never guessed as zero. |

## Where it is written

`agent/loop.py:_llm_chat` is the one seam both backends pass through, so it is
the one write site. Each backend attaches its counts to the returned message
under a private `_usage` key; `_llm_chat` times the call, **pops** that key, and
records the row.

The pop matters: `advance()` appends the returned message straight into the
history it re-sends, so a leaked `_usage` key would be fed back to the model as
part of its own prior turn. The test asserts both halves — the row is written
*and* the returned message has no `_usage` key.

Claude Code rows come from `tasks/build_worker.py` instead, off the JSON that
`claude -p --output-format json` prints. That path never estimates: it passes
through `total_cost_usd` (or per-model `costUSD`) because Anthropic already told
us the real number.

Accounting can never kill a turn. `record()` wraps its whole body in
`try/except` and swallows; a broken ledger costs a row, not a chat reply.

## Retention

Pruned on write, not rotated. Past `WREN_USAGE_MAX_BYTES` (default 5MB) the file
is rewritten inside the same lock keeping only rows newer than
`WREN_USAGE_RETENTION_DAYS` (default 90). At Wren's call rate 90 days sits well
under 5MB, so the size cap is the backstop, not the usual trigger.

The file is `.jsonl` on purpose: `chat/logview.py` globs `logs/*.log`, and this
is structured data that would be nonsense in the log viewer. That also means
neither `.gitignore` log glob covers it, so it carries its own line.

## Prices go stale

`_PRICES` in `agent/usage_ledger.py` is hand-maintained USD per million tokens,
keyed by model-name prefix, longest match wins. Nothing fetches it. When a
Gemini price changes, or `WREN_GEMINI_MODEL` moves to a model that is not listed,
the cost card starts counting *unpriced calls* — that count is the signal to
edit the table. It is shown next to the cost for exactly that reason.

## Reading it

`chat/usage.py` reads Wren's ledger plus each sibling's, found through
`WREN_EXTERNAL_TASK_ROOTS` — the same mechanism that already federates their
launchd runs onto the dashboard. Knowledge still runs one way: it reads their
files as data, and neither sibling knows Wren exists. A sibling with no ledger
yet reads as no rows, never as an error.

Rows older than the window are discarded on a *string* comparison against the
cutoff, before any date parsing — ISO-8601 sorts lexicographically and the file
is append-ordered, which is what keeps a year of history cheap to skip. Results
are cached on every ledger's (mtime, size), so the cache invalidates the moment
any agent appends.

## The siblings

ScribeJay and ObsidianWikiAgent are not instrumented yet. Each needs the same
writer called at its own backend seam, with its own `agent` value, writing into
its own checkout. Nothing in this repo does that work — the handoff docs in
`docs/handoff/` are carried into those repos and run there.
