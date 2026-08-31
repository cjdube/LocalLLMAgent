# Wren's limits — where they are, why they exist, what to do when you hit one

Wren runs on a small local model. Almost every number in this document exists
because that model has a fixed-size context window, and overflowing it does not
raise an error — it silently produces a wrong answer. This file is the map: what
each limit is, where it lives, how its value was chosen, and what happens when
something hits it.

**The one-sentence version:** everything below is downstream of `OLLAMA_NUM_CTX`.
That number is the model's context window. Every other cap exists to keep a
prompt from reaching it.

---

## Why the limits exist at all

Three distinct problems, often confused:

| Problem | What goes wrong | Which limits address it |
|---|---|---|
| **Context overflow** | Prompt exceeds `num_ctx`. Ollama truncates the **front** — where the system prompt lives. The model then loses its identity and its rules, and often runs away in a repetition loop. | history trimming, tool-result caps, per-tool char budgets, prompt compaction |
| **Output budget exhaustion** | Generation exceeds `num_predict`. On a thinking model this returns **empty content**, not a truncated answer. | `OLLAMA_NUM_PREDICT`, `think=False`, `WREN_GEMINI_THINKING_BUDGET` |
| **Resource / safety bounds** | A runaway loop, a slow API, an unbounded store, a brute-force login. | iteration caps, HTTP timeouts, store pruning, login throttle |

Only the first two are "small model" limits. The third group would exist on any
system and is listed separately at the end.

---

## Where limits are defined — the three tiers

Wren uses a consistent pattern. Knowing which tier a number is in tells you how
to change it.

### Tier 1 — Environment variables (`config/.env`)

Tunable without a code change. Every one is documented in
`config/.env.example`. These are the numbers you actually turn.

### Tier 2 — Module constants (`UPPER_CASE` at the top of a file)

Hard-coded, but deliberately placed at module top with a comment explaining the
measurement that produced the value. Changing one is a code change and a test
run. Most of Wren's limits live here.

### Tier 3 — Inline slices (`text[:300]`)

A few short, obvious truncations (a 60-char title, a 100-char log preview).
Not tunable, not important. Not catalogued exhaustively below.

**Convention worth knowing:** a Tier 2 constant is only *documented* in the code
comment. There is no central registry — this document is the closest thing, and
it will drift. When a comment and this file disagree, the code comment wins.

---

## Tier 1: the environment variables

Live values are from `config/.env` on the Mac mini as of 2026-08-16. "Default"
is what the code falls back to when the variable is unset.

### The model call

| Variable | Default | Live value | What it bounds |
|---|---|---|---|
| `OLLAMA_NUM_CTX` | 8192 | **32768** | Context window in tokens. **The root number.** Ollama otherwise falls back to ~4096 and silently front-truncates. |
| `OLLAMA_NUM_PREDICT` | 3072 | unset (3072) | Tokens generated per call. Bounds a repetition loop. Sized comfortably above the longest legitimate reply (~2200 tokens for an app teardown). |
| `OLLAMA_MAX_TOOL_RESULT_CHARS` | 8000 | 8000 | One tool result before it enters the conversation. ~8000 chars ≈ 2000 tokens. |
| `OLLAMA_TIMEOUT` | 300 | 300 | Read timeout (seconds) for a scheduled-task model call. Between-chunks, not total — it covers the wait for the first token (prefill of a large prompt runs ~50s). |
| `OLLAMA_WARM_TIMEOUT` | 600 | unset | Timeout for the `warm_model()` preload, so a cold ~17GB load gets its own budget instead of eating the generation's. |
| `OLLAMA_KEEP_ALIVE` | `30m` | unset | How long the model stays resident. Keeps the day's scheduled tasks off a cold load. |

### Chat

| Variable | Default | Live value | What it bounds |
|---|---|---|---|
| `WREN_CHAT_MAX_HISTORY_CHARS` | 16000 | **48000** | Conversation history re-sent each turn. At ~4 chars/token, 48000 ≈ 12k tokens. |
| `WREN_CHAT_MODEL_TIMEOUT` | 120 | unset | Read timeout for an *interactive* turn. Deliberately tighter than `OLLAMA_TIMEOUT`: someone is waiting, and a fast "Ollama is busy" beats a five-minute spinner. |

> **These two move together.** `OLLAMA_NUM_CTX` and `WREN_CHAT_MAX_HISTORY_CHARS`
> are coupled — the `.env.example` comment and AGENTS.md both say raise them
> together. The 8192/16000 default pair and the live 32768/48000 pair are both
> internally consistent.

### The cloud backend (opt-in)

| Variable | Default | Live value | What it bounds |
|---|---|---|---|
| `WREN_GEMINI_MAX_OUTPUT_TOKENS` | 8192 | unset | Cloud model output per call. **This is the limit that actually bounds a Gemini call** — see the note below. |
| `WREN_GEMINI_THINKING_BUDGET` | 0 | **128** | Thinking tokens. A hint, not a cap, and not portable across models. See below. |

> **The Gemini thinking budget is the one limit in this document that does not
> do what its name says.** Measured 2026-08-16 against the live API:
>
> - **It is not enforced.** `gemini-3.7-flash` spent 268-313 thinking tokens with
>   the budget set to **0**. Both 3.6 and 3.7 overran a budget of 128 (109-397).
>   So "0 = thinking off" is false on the 3.x models.
> - **It is not portable.** 0 works on `gemini-2.5-flash` and `gemini-3.7-flash`,
>   but `gemini-2.5-pro` and `gemini-3.6-flash` reject it with a bare
>   `400 INVALID_ARGUMENT`. 128 and -1 are accepted everywhere.
> - **`WREN_GEMINI_MAX_OUTPUT_TOKENS` is the real bound.** Thinking counts
>   against it. At `max_output_tokens=200`, thinking consumed 194 and the call
>   returned 3 characters with `finish_reason=MAX_TOKENS` — the documented
>   empty-answer failure, reproduced. At the 8192 default there is ample room.
>
> Practical upshot: size `WREN_GEMINI_MAX_OUTPUT_TOKENS` for the answer you need
> and leave the budget pinned at 128, which is portable and survives a rollback.

### Task-specific prompt bounds

| Variable | Default | What it bounds |
|---|---|---|
| `WEB_FETCH_MAX_CHARS` | 8000 | Markdown returned by `fetch_webpage`. Matches the loop's cap deliberately. |
| `GOOGLE_HTTP_TIMEOUT_S` | 30 | Google API calls. |
| `OPP_STALLED_DAYS` | 45 | Days a watched opening stays open before it's flagged stalled. |
| `OPP_SCORE_THRESHOLD` | 8 | Minimum model score (1-10) that adds a phone push to the email. |

---

## Tier 2: the module constants, by layer

The layers nest. A wiki page is trimmed by the tool, then possibly trimmed
again by the loop, then contributes to a history that is trimmed by the server.

### Layer A — the agent loop (`agent/loop.py`)

| Constant | Value | Why this value |
|---|---|---|
| `MAX_TOOL_ITERATIONS` | 10 | Model round-trips in one turn. Was 6, which suited single-fetch tools; navigating the wiki (`search_wiki` → several `read_wiki_page` → answer) legitimately chains more. |
| `MAX_GATED_PAUSES_PER_TURN` | 3 | Confirmation-gated writes one user-turn may pause on. `MAX_TOOL_ITERATIONS` bounds the loop *inside* one `advance()` call, but a gated call returns out of `advance()` entirely — so the counter reset on every continuation and the pause/resolve chain had no bound at all. A gated call identical to one already answered in the turn is also never re-offered; `_answered_gated_calls` in the same module is what remembers them. |
| `MAX_TOOL_RESULT_CHARS` | 8000 (env) | See above. |
| `TOOL_RESULT_CHAR_CAPS` | per-tool dict | Overrides for tools returning **one curated document of known size**, where the flat cap actively misleads. |

`TOOL_RESULT_CHAR_CAPS` currently holds two entries, both with a real incident
behind them:

- `read_wiki_page: 16000` — 7 of the vault's 390 wiki pages exceed 8000 chars.
  The flat cap handed back 42-95% of a page, and Wren answered "SVPG isn't in
  your wiki" about a page whose SVPG section was in the cut part.
- `fetch_webpage: 9500` — not extra content room. `fetch_webpage` already caps
  its markdown at 8000; this is room for the **JSON wrapper around it**. Without
  it, every truncated fetch landed ~520 chars over and the loop re-trimmed a
  result the tool had already trimmed on purpose.

> **The pattern to copy:** when a tool bounds its own output, its entry here must
> sit *above* the tool's internal budget. The tool trims first, deliberately,
> keeping its footer and naming what it dropped. This cap is only a backstop —
> if it fires, it undoes that careful trim.

#### Why the gated-pause bound is code, not prompt wording

One "add a calendar event" produced four confirmation cards, and *declining* fed
the next one: a decline came back as `{"error": ...}`, the same shape a crashed
tool returns, which a model correctly retries. Two rules follow.

First, a tool result must carry `tool_name` and say what it wrote. Without that
the model cannot tell its own call ran, so it calls again.

Second, the bound belongs in `agent/loop.py`, not in the prompt. The
model-level trigger never reproduced in 16 live runs after the prompt was
reworded, so the wording is not evidence of a fix —
`MAX_GATED_PAUSES_PER_TURN` and `_answered_gated_calls` are the guarantee.

### Layer B — the chat server (`chat/server.py`)

| Constant | Value | Why |
|---|---|---|
| `MAX_MESSAGE_CHARS` | 8000 | One user message. |
| `MAX_HISTORY_CHARS` | 16000 (env; 48000 live) | History budget. Trimming drops **oldest whole user-turns**; system prompt and newest turn are always kept. |
| `CHAT_MODEL_TIMEOUT` | 120 (env) | See above. |
| `MAX_CONTENT_LENGTH` | 256 KB | Flask request body, sized for a chat turn. |
| `SESSION_IDLE_EVICT_S` | 86400 | In-memory session eviction. |
| `_CHARS_PER_TOKEN` | 4 | The estimate used by the startup budget check. |

**The startup budget check** is the safety net worth knowing about. On boot the
server prices out the worst case:

```
worst_case = MAX_HISTORY_CHARS + prompt_head + (MAX_TOOL_ITERATIONS × MAX_TOOL_RESULT_CHARS)
capacity   = OLLAMA_NUM_CTX × 4
```

If `worst_case > capacity` it logs **and pushes** a warning naming both numbers.
This exists because history trimming only bounds the space *between* turns — a
turn's tool results pile on top of it. Raising `WREN_CHAT_MAX_HISTORY_CHARS`
without a matching `OLLAMA_NUM_CTX` raise would otherwise silently truncate the
system prompt.

**`prompt_head` is the term to understand**, because it is the biggest one and
it is the only one with no env var attached. It is everything on every turn that
is not conversation: the system message (persona, identity, the date paragraph,
the skills / lens / tool-group indexes, and the pinned-memory block), the
compaction summary that rides inside it, and the tool schemas priced with every
group loaded. `_prompt_head_chars()` measures it at boot rather than storing a
number, because the schema total moves whenever a tool is registered.

Until 2026-08-26 the check did not count it at all, and so reported "fits" on a
config that did not fit — the terms it counted were exactly the ones with env
vars attached, which is a tidy illustration of measuring what is easy to name.
At the live values:

```
prompt head  =  48,755      (system message + summary + all-groups schemas)
history      =  48,000
tool results =  80,000      (10 × 8,000)
worst case   = 176,755
capacity     = 196,608      (49152 × 4)
                 19,853 spare — about 10% headroom
```

`OLLAMA_NUM_CTX` moved from 32768 to 49152 on the same day for this reason: at
32768 the true worst case was 45,683 chars **over** capacity, while the old
arithmetic put it 1,572 under. The worst case still needs all ten tool
iterations to return full-size results, so it is a ceiling rather than a typical
turn — which the measured distribution below is there to keep in proportion.

**What real turns actually use.** Over 465 logged chat turns at
`OLLAMA_NUM_CTX=32768`: median 6,506 tokens (20% of num_ctx), p95 13,932 (43%),
peak 27,033 (82%). Nothing overflowed on that config, and prompt compaction has
fired once ever. The peak is the number that matters: one turn climbed 12,214 →
27,033 tokens across 8 tool steps, and `MAX_HISTORY_CHARS` governed none of that
climb — `_trim_history` runs before a turn, never inside it.

### Layer C — inside each tool

This is where most of the constants are. Nearly all follow the same shape.

> **The rule this layer teaches:** *a count cap never bounds size — pair every
> count cap with a char budget, and measure it against real data.* Five separate
> tools needed a char budget added after their row cap alone proved insufficient.
> `nudges.py` is the clearest case: 25 rows was assumed short, real nudges ran to
> 9480 chars, and every row is paid for **twice** — once raw, once rendered into
> `summary`.

| Tool | Count cap | Char budget | Note |
|---|---|---|---|
| `wiki.read_wiki_page` | — | `MAX_PAGE_CHARS` 14000 | Below its 16000 loop cap on purpose. Keeps the `[[link]]` footer. |
| `wiki.search_wiki` | `MAX_SEARCH_RESULTS` 20 | `MAX_SEARCH_CHARS` 4000 | `_HEAD_CHARS` 2048 per page read. |
| `wiki` lens index | `MAX_INDEX_LENSES` 8 | `MAX_INDEX_CHARS` 600 | Char budget usually bites first: descriptions run 150-200 chars, so ~3 exhaust it. |
| `calendar` | `MAX_CHAT_EVENTS` 50 | `MAX_CHAT_EVENT_CHARS` 6000 | |
| `chrome_history` | `MAX_CHAT_SITES` 60 | `MAX_CHAT_SITE_CHARS` 6000 | `_MAX_PATH_CHARS` 120 per path. |
| `google_tasks` | `_MAX_FETCH` 1000 | `_MAX_CHAT_TASK_CHARS` 5500 | Fetch cap stops a runaway account spinning. |
| `nudges.list_nudges` | `MAX_CHAT_ROWS` 25 | `MAX_CHAT_CHARS` 5000 | `MAX_DAYS` 90, `DEFAULT_DAYS` 14. |
| `opportunities` | `_LIST_LIMIT` 20 | `_LIST_CHARS` 5500 | |
| `push_log` | `DEFAULT_LIMIT` 20, `MAX_LIMIT` 100 | — | `MAX_DAYS` 30. |
| `skills` index | `MAX_INDEX_SKILLS` 12 | `MAX_INDEX_CHARS` 800 | |
| `memory` pinned block | `MAX_ACTIVE_MEMORIES` 20 | `MAX_MEMORY_BLOCK_CHARS` 1500 | Uncapped until 2026-08-26, which left the prompt head unpriceable. Sized ~7x the live store (3 active facts, 203 chars) so it should never bite; if it does, the WARNING names the dropped ids. |
| `projects` | `MAX_DOC_TITLES` 40 | `DOC_CHARS` 2000 | **The two are not a pair** — see below. `DOC_CHARS` bounds the README and agent-instructions *bodies*; nothing bounds the titles. |
| `evaluate_app` | — | `_CONTENT_CHARS` 6000 | |
| `evaluate_against` | — | `_LENS_CHARS` 12000, `_TARGET_CHARS` 16000, `_FETCH_CHARS` 20000 | Fetches 20000 because compaction loses 20-30%; fetching exactly 16000 would land under it. `_LENS_CHARS` is 12000, not the 8000 the docs once said. |
| `research` | — | `_SNIPPET_CHARS` 400 | |
| `notify` | — | `_MAX_MESSAGE_CHARS` 500 | ntfy body. |
| `toolset` email preview | — | `BODY_PREVIEW_CHARS` 240 | |
| `weather` | `MAX_DAYS` 5 | — | Not a model limit — it's the ceiling of OpenWeatherMap's free endpoint. |
| `github_starred` | `MAX_PAGES` 10, `MAX_ENRICH` 15 | — | API-call bounds, not context bounds. |

#### What `MAX_DOC_TITLES` actually guards

Not the context window. `project_scan` distils **one project per model call**, so
project count never drives prompt size, and the biggest real prompt today is
LocalLLMAgent's at ~5100 chars against a 32768-token `num_ctx`. Nothing here is
close to overflowing.

What it guards is the **signal ratio inside that one prompt**. The model is asked
what a project *is*, and the README is the part that answers; doc titles are
supporting detail. A project with a 300-page `docs/` tree would contribute
~10000 chars of titles against a README capped at 2000, and the answer would be
distilled off a table of contents instead of off the pitch. The cap keeps the
titles a minority of their own prompt.

It does **not** bound the anchor `daily_synthesis` matches against. That anchor is
name + summary + topics only (`MAX_TOPICS` 15, `MAX_SUMMARY_CHARS` 300); doc
titles never reach it. They exist only to inform the distillation.

Measured 2026-08-23 across the real `PROJECTS_DIR`: exactly one checkout has a
`docs/` tree at all — LocalLLMAgent, 32 titles, 1018 chars total, longest title
75 chars, ~32 chars average. So the cap is nowhere near binding on anything but
this repo, which is also the only repo that has ever tripped it (at 21, and again
at 32).

**Known gap: the count cap has no char budget.** A title is a heading line copied
whole (`_doc_titles`) with no per-title truncation, so 40 titles is 40 lines of
unknown length, not a bounded number of characters — the exact shape this
document's own preamble warns about. Real data makes it a remote risk (75 chars
is the worst heading in the tree) and it has never fired, so it is recorded here
rather than fixed. If it ever does fire, the fix is a per-title cap plus a total,
matching the other rows in this table.

### Layer D — scheduled tasks

Tasks use `complete_text()` (one-shot, no tools), so they have no history or
tool-result problem. Their limits bound the **input** they compact into a prompt.

| Task | Constants |
|---|---|
| `agent/activity_log.py` | `MAX_CHROME_SITES` 40, `MAX_YOUTUBE_VIDEOS` 25, `MAX_YOUTUBE_DESC_CHARS` 500, `MAX_PAGES_PER_SITE` 6 |
| `daily_synthesis` | `MAX_NUDGES` 3, `MAX_ANCHOR_CANDIDATES` 5, `MAX_CROSS_CANDIDATES` 3, `MAX_ANCHOR_SUMMARY_CHARS` 200, `MAX_PROJECT_ANCHOR_TOKENS` 40, `MAX_AI_CHAT_BULLETS` 20, `RECENT_NUDGE_DAYS` 7, `MAX_SYNTHESIS_ATTEMPTS` 2 |
| `opportunity_digest` | `MAX_SCORE_ITEMS` 40 (batch size — one model call per batch), `_SNIPPET_CHARS` 300, `_EDGAR_MAX_PAGES` 3, `_HN_MAX_PAGES` 3, `_EDGAR_DEFAULT_LOOKBACK_DAYS` 7 |
| `project_scan` | `MAX_TOPICS` 15, `MAX_SUMMARY_CHARS` 300 (kept under `daily_synthesis`'s own 200-char anchor cap so it doesn't waste prompt budget) |
| `starred_blurbs` | `README_CHARS` 2000 |
| `log_inspector` | `MAX_DETAIL_LINE` 150, and it fits its whole push inside `notify._MAX_MESSAGE_CHARS` |
| `bg_worker` | `MAX_TRANSIENT_ATTEMPTS` 3 |

**`MAX_SYNTHESIS_ATTEMPTS = 2` is a different kind of limit** — it's a retry, not
a cap. `daily_synthesis` keeps thinking **on** (it must reason past its prompt),
and lost its output to an empty response in 3 of its first 29 runs. The retry
warns even when it succeeds, so the failure rate stays visible.

### Layer E — a limit that isn't a number

**Lazy tool loading** (`docs/tool-loading.md`) is a context limit expressed as
structure rather than a constant. Wren has 57 tools, about 40,000 chars of
schema in total; chat sends ~16,000 of that as a 24-tool always-loaded core. Sending
every schema on every turn wastes context the small model can't spare.
Chat sends a small always-loaded core plus groups pulled in on demand. Applies to
chat only — the background worker uses the whole registry.

---

## Limits that are not about the model

Listed for completeness, so they don't get confused with the ones above.

| Where | Limit | Purpose |
|---|---|---|
| `chat/login_throttle.py` | `MAX_FAILURES` 5, `BASE_LOCKOUT_S`, `MAX_LOCKOUT_S` 900, `MAX_TRACKED` 1000 | Brute-force defence with exponential backoff. |
| `chat/logview.py` | `WINDOW_BYTES` 512,000; `DEFAULT_LIMIT` 300; `MAX_LIMIT` 1000; `MAX_MSG_CHARS` 4000; `MAX_EXTRA_LINES` 200 | Bounded reads of append-only launchd logs that nothing rotates. |
| `chat/routes_games.py` | `AI_TIMEOUT_S` 160, `WARMUP_TIMEOUT_S` 620, `MAX_GAME_BODY_BYTES` 2 MB | Proxy timeouts must stay **above** the browser's own budgets. |
| `agent/escalations.py` | `MAX_RECORDS` 200 | Store pruning. |
| `agent/tools/push_log.py` | `_PRUNE_AFTER_DAYS` 30, `_MAX_ROWS` 500 | Age first, then a hard cap so a burst can't outrun the age window. |
| `agent/tools/background.py` | `_TOKEN_MAX_AGE_S` 3600, `_MAX_APPROVAL_REPUSHES` 3, `_EXPIRE_AWAITING_AFTER_S` 86400, `_MAX_NONTERMINAL_JOBS` 100, `_MAX_STORED_JOBS` 500, `_LIST_LIMIT` 20 | Bounded approval reminders, job lifetime, polling store, and model-facing list. |
| `chat/insights.py` | `_MEMORY_TEXT_MAX` 300, `_WIKI_PAGES_MAX` 150 | Dashboard rendering only. |
| HTTP timeouts | `web_fetch` 60s, `research` 15s, `opportunity_digest` 15s, `notify` 10s, `projects` git 10s, `games` probe 0.3s | Policy: **every HTTP call has an explicit timeout.** |
| `launchd/local.wren.selfheal.plist` | `StartInterval` 3600 | How long a Homebrew Python upgrade can sit unrepaired. See below. |

### The self-healer's one-hour window

`reload-after-upgrade.sh` fingerprints the venv interpreter and reloads every
agent that execs it as soon as that fingerprint changes — so an upgrade is
normally repaired before any job has to fail. But the check only runs when
`local.wren.selfheal` fires, and that is hourly.

**The residual limit: a job whose scheduled fire time falls inside the gap
between the upgrade and the next self-heal pass still dies at launch.** It gets
caught by the older per-job `needs LWCR update` check on the following pass and
placed into the serialized startup-recovery queue. The queue waits for its
dependency and runs one task at a time, so the cost is a delay rather than a
boot-time fan-out or a lost week. See [reboot-recovery.md](reboot-recovery.md).

Do not close this by shortening the interval. The window is already small, the
recovery is automatic, and the check costs a `launchctl` query per agent.

---

## What happens when a limit is hit

The design goal is that **no limit is ever hit silently.** Progress toward that
goal is uneven; here is the honest state.

### Well handled — it says so

| Limit | Behaviour |
|---|---|
| Tool result over cap | Appends `... [truncated N chars to fit the context window]` **into the result the model reads**, and logs a WARNING with the tool name, overage and cap. The model can therefore say the answer is partial. |
| Per-tool count/char caps | The tool reports the **true total** alongside the shown count, and names which cap bit — e.g. skills says "the 800-char budget" vs "the 12-skill cap". This was a real bug class: a summary that counted the rows it had already sliced reported the shown count as the real one. |
| Chat history trim | Drops oldest whole user-turns and tells the user the history was trimmed. |
| `num_ctx` reached | `loop.py` compares reported `prompt_tokens` to `num_ctx` and logs a WARNING that the front of the prompt was likely truncated. |
| `num_predict` reached | Compares `eval_tokens` to `num_predict` and logs a WARNING that the reply was cut off. Gemini's `MAX_TOKENS` finish reason mirrors it. |
| Startup budget overflow | Logged **and pushed** to the phone at boot with both numbers and the fix. |
| `MAX_TOOL_ITERATIONS` | Raises `RuntimeError`, naming the constant. Loud by design. |
| `MAX_GATED_PAUSES_PER_TURN` | The call is **not executed**, and the model gets a tool result saying so and telling it to answer in words. Logged at WARNING naming the tool and whether it was capped or a repeat. The user sees a normal reply rather than a fourth confirmation card. |
| EDGAR page cap | Logs that the per-state safety bound was hit, so a truncated poll isn't mistaken for a quiet week. |
| `daily_synthesis` empty response | Retries once and warns **even on success**, so the failure rate stays measurable. |

### The dangerous case

An **empty** model response is the failure that looks like nothing went wrong.
Thinking tokens share the `num_predict` budget with the answer, so a call that
reasons too long returns empty content — and the caller reports it as a parse
failure, pointing at the wrong thing. Hence the rule: pass `think=False` for any
call that fills a template, and pass `logger=` with it so the cut-off warning
surfaces. Measured on 40-lead scoring: thinking on, 0 of 3 runs produced output;
off, 3 of 3 and 5x faster. Full evidence in `docs/model-constraints.md`.

### Who notices

`tasks/log_inspector.py` (daily 8:00 AM) scans the last 24h of `logs/*.log` for
errors **and the small model's strain signals**, and pushes only when something
needs attention. It's pure Python — a health check that called the model
couldn't report that the model is down. That's the backstop that turns a logged
WARNING into something you actually see.

---

## How the values were chosen

There is no formula. Four methods, in rough order of how much trust the number
deserves:

1. **Measured against real data.** The strongest. `nudges` 5000 chars (25 rows
   measured at 9480), `evaluate_against` `_FETCH_CHARS` 20000 (compaction loses
   20-30%), `fetch_webpage` 9500 (every truncated fetch landed ~520 over),
   `read_wiki_page` 16000 (7 of 390 pages exceed 8000), the session block gap of
   20 minutes (10 fragments vs 30 over-merges), `think=False` (0 of 3 vs 3 of 3).
2. **Derived arithmetically.** `MAX_HISTORY_CHARS` from `num_ctx` at 4 chars per
   token; `MAX_TOOL_RESULT_CHARS` 8000 ≈ 2000 tokens, "a few still fit".
3. **An external ceiling.** `weather.MAX_DAYS` 5 is OpenWeatherMap's free
   endpoint. Not ours to tune.
4. **A reasonable guess that hasn't been challenged.** Most of the count caps —
   50 events, 60 sites, 20 opportunities. These were plausible when written and
   have never been measured. The char budget beside each one is what actually
   holds the line, which is why the char budgets got measured and the row counts
   didn't.

**The honest summary:** the numbers that hurt got measured. The rest are round
numbers. That's fine, because the char budget is the real bound and the count cap
is mostly a cheap first filter.

---

## Raising a limit safely

**Before you change anything, check whether the limit is actually the problem.**
Most of them report themselves now — read the log line, don't guess.

### To give the model more room overall

Raise `OLLAMA_NUM_CTX` **and** `WREN_CHAT_MAX_HISTORY_CHARS` together in
`config/.env`. Then:

1. `ollama stop <model>` — a resident model does **not** pick up a `num_ctx`
   change. (The Ollama app's own context slider does not apply to Wren.)
2. Restart the chat server. It's launchd `KeepAlive` — never `python -m
   chat.server` by hand.
3. Watch the startup log. If the worst case now exceeds capacity, the server
   says so at boot and pushes it to the phone.

Costs: more RAM per request, slower prefill (the interactive `CHAT_MODEL_TIMEOUT`
of 120s has to cover it), and one Ollama slot shared by four consumers.

### To let one tool return more

Raise the tool's own internal budget first, then its `TOOL_RESULT_CHAR_CAPS`
entry in `agent/loop.py` to sit **above** it. Wrong order and the loop's backstop
undoes the tool's deliberate trim, cutting off the footer that names what was
dropped.

### To let one scheduled task read more

Raise its own `MAX_*_CHARS`. Scheduled tasks are one-shot with no tool results,
so they're bounded by their compaction step alone — much more headroom than chat.

### Always

Run `pytest` (`.venv/bin/python -m pytest` — bare `python` is absent and system
`python3` is 3.9). Several caps are pinned by tests. Then verify against the
**live** model: pytest monkeypatches every model call, so it cannot see an empty
or truncated response. Replay real input three times and read `done_reason` /
`finish_reason`.

---

## Related

- [docs/model-constraints.md](model-constraints.md) — the incident evidence
  behind the small-model rules
- [docs/ollama-serving.md](ollama-serving.md) — one Ollama, one slot, four
  consumers
- [docs/tool-loading.md](tool-loading.md) — the context limit expressed as
  structure
- [docs/llm-backend.md](llm-backend.md) — the cloud backend and its own budgets
- [docs/log-inspector.md](log-inspector.md) — what notices a limit was hit
- `config/.env.example` — every tunable, with its reasoning
