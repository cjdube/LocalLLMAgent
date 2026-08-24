# Comparing local models

Wren runs whichever model `OLLAMA_MODEL` names. Swapping it is one line, but
deciding *which* model is not — a public leaderboard measures generic
tool-calling on generic schemas, and none of Wren's recorded failures were
generic. `evals/` answers the question with Wren's own prompts instead.

## Why not a public benchmark

Every failure in [model-constraints.md](model-constraints.md) shares one shape:
the model returned something well-formed, the job exited 0, and the answer was
wrong. Those bugs live in the interaction between a specific model and a
specific prompt — an 8,800-character system prompt, a lazily-loaded subset of
~25 tools, `num_ctx=32768`, `num_predict=3072`. A benchmark that sends a bare
prompt and a hand-written schema is measuring a different system.

Two design rules follow, and they are the reason this harness is thin:

- **It calls production's functions, not Ollama.** `agent.loop.advance()` for
  chat, `agent.loop.complete_text()` for the scheduled tasks, with the system
  prompt from `chat/server.py:_system_message_content()` and the tool subset
  from `agent.toolset.tools_for(groups_for_message(...))`. A parallel client
  would silently test different settings.
- **It runs at production settings, not at temperature 0.** `_ollama_chat`
  sets no temperature, so pinning one here would measure a configuration that
  never runs — and would hide run-to-run variance, which is the actual risk.
  `daily_synthesis` lost its whole output in 3 of its first 29 runs with
  nothing changed.

## The two paths

**Chat** (`evals/cases_chat.py`) — one user message through `advance()` with
canned tool results fed back. Scored on: was a tool called, was it the right
one, were the arguments right, did the final answer use the result, and did the
model invent anything. Three cases are lifted straight from recorded incidents
(`calendar_weekday`, `games_vague`, `chain_strava_log`) and three expect *no*
tool at all.

Scoring the final answer matters as much as scoring the call. The weekday bug
was a **correct** tool call followed by a wrong answer, so a harness that stops
at "did it emit a tool_call" would have scored that run as a pass.

**Scheduled tasks** (`evals/cases_tasks.py`) — the real system prompt and real
parser from seven `complete_text()` callers, over fixture input sized like the
real thing (the scoring case uses 40 leads because 40 is what broke). Scored on:
was the answer non-empty, did it parse, and did N items in produce N results
out.

This path exists because of the thinking budget. Thinking tokens come out of
the same `num_predict` as the answer, so a model that reasons too long returns
**empty content** — and a tool-calling benchmark cannot see that at all. It is
the likeliest way a reasoning-first candidate model differs from an incumbent.

## Running it

```
.venv/bin/python -m evals.run_eval --models gemma4:26b-mlx qwen3.6:27b-mlx
.venv/bin/python -m evals.score
```

`--reps` defaults to 3. One rep measures nothing about variance, which is
mostly what this is for. `--path chat|tasks|both`, `--only <case ids>` and
`--timeout` are the other flags.

Ollama serves **one request at a time** ([ollama-serving.md](ollama-serving.md)),
so while this runs, chat stalls and scheduled tasks queue behind it. Run it
overnight or over a long break. Budget 90 minutes to 2 hours per model.

Models are looped outermost and `warm_model()` is called once per model, so a
17-19GB load is paid once per arm rather than once per case. Raw records are
written after each model finishes, so a wedged runner partway through doesn't
cost the arm that already completed. Between models, `ollama stop` the resident
one.

## Reading the output

`evals.score` prints three things. The rate table is the headline, but the
**per-case grid** is what the decision should rest on: it shows reps passed per
case (`2/3`, `3/3`) and flags any case a model passes inconsistently. A model
that is right two times in three is a different proposition from one that is
right three in three, and an aggregate percentage hides exactly that.

Then read the raw failures. A loss caused by a fixable prompt wording is not
the same as a loss caused by the model's shape, and no rate can tell them
apart. The failure list prints what the model actually called, what it
fabricated, and any WARNING `agent/loop.py` emitted during the run
(prompt truncation, `num_predict` cut-off) — those warnings are direct evidence
for the thinking-budget failure.

## Run of 2026-08-15 — gemma4:26b-mlx vs qwen3.6:27b-mlx vs gemma4:31b-mlx

234 runs, 3 reps, production settings. **Outcome: stayed on `gemma4:26b-mlx`.**

| | right tool | args right | used the result | median chat |
|---|---|---|---|---|
| gemma4:26b-mlx | 100% (48/48) | 100% (21/21) | 100% (36/36) | 3.5s |
| qwen3.6:27b-mlx | 100% (48/48) | 100% (21/21) | 94% (34/36) | 15.7s |
| gemma4:31b-mlx | 100% (48/48) | 100% (21/21) | 100% (36/36) | 8.5s |

**Tool calling did not separate them.** All three chose the right tool with the
right arguments on every run, including the three cases lifted from past
incidents. If a future comparison is being made on tool-calling grounds alone,
this is the number to check first — at this size, it saturates.

What separated them was everything after the tool call:

- **qwen3.6 returns an empty chat reply.** It calls the tool, receives the
  result, and produces nothing: 5 empty replies in 11 runs of `tasks_due_soon`.
  `done_reason` is `stop`, not `length`, at ~65-103 eval tokens, so
  `_ollama_chat`'s cut-off warning never fires. It also emits a separate
  `thinking` field that `_ollama_chat` discards by design — 1,105 tokens to
  answer "say hello in five words".
- **qwen3.6 volunteers weekdays and gets them wrong.** Handed `2026-08-18` by
  the tool, it added a weekday unprompted in 4 of 8 runs and was wrong in 2 of
  those 4 (said Monday; it is a Tuesday). That is the failure class
  [model-constraints.md](model-constraints.md) already fixed at the tool
  boundary, re-entering through the prose. No tool contract prevents it.
- **gemma4:26b is weak on `daily_synthesis`** — empty content on 2 of 3 single
  calls, the one case that runs with thinking ON. Production's
  `MAX_SYNTHESIS_ATTEMPTS = 2` absorbs part of that, so the effective rate is
  lower than 2-in-3. qwen3.6 and gemma4:31b were 3 of 3.

`gemma4:31b-mlx` failed nothing at all and fixes the synthesis weakness, but
costs 2.4x on chat latency (3.5s -> 8.5s median, 99s worst case), paid on every
message. Kept 26b; 31b is the fallback if synthesis flakiness becomes the
bigger annoyance.

One finding that outlived the comparison: an empty final chat reply is logged
**nowhere**. `_warn_if_promised_without_acting` returns early on `not text`,
and loop.py only warns at the `num_predict` cap. The user sees silence and the
log shows a normal turn.

### qwen3.8:27B — deferred, not rejected (2026-08-16)

Started and **aborted after 12 of 78 runs**. `qwen3.8:27B` is a Q4_K_M **GGUF**
build; the other three arms are **MLX**. It ran 30-142s per case against
gemma4:26b-mlx's 3.5s median.

Don't read that as a verdict on the model. The gap decomposes into two costs,
only one of which belongs to qwen3.8:

- its family is already slow on MLX — `qwen3.6:27b-mlx` measured 15.7s median,
  4.5x gemma4:26b, which is the thinking-token cost
- the GGUF packaging adds roughly another 4-6x on top of that on Apple silicon

Comparing one GGUF arm against three MLX arms measures the packaging, not the
model, so the run was stopped rather than published. It passed all 12 completed
cases on correctness (right tool, right arguments, used the result) — one rep,
weak evidence, but nothing suggesting it is worse.

**Re-run it when an MLX build ships.** One command, no code changes:
`--models qwen3.8:27b-mlx gemma4:26b-mlx`, then merge or compare against the
existing raw file. Done on 2026-08-24, below.

## Run of 2026-08-18 — gemma4:26b-mlx vs gemma4:12b-mlx

156 runs, 3 reps, plus a 10-run re-measure of the date cases. Asked whether a
**smaller** model could take over the everyday work and leave 26b for the few
calls that judge — 12b is 7.7GB against 17GB, and freeing that is what would
make headroom for `OLLAMA_NUM_PARALLEL=2`
([ollama-serving.md](ollama-serving.md)).

**Outcome: 12b held everything the harness can measure.** No switch has been
made — the harness does not cover multi-turn chat, and nothing here judges
whether a *synthesis* is any good, only that one came back.

| | right tool | args right | non-empty task answer | median chat | slowest chat | median task |
|---|---|---|---|---|---|---|
| gemma4:26b-mlx | 100% (48/48) | 100% (21/21) | 95% (20/21) | 5.9s | 119.1s | 14.6s |
| gemma4:12b-mlx | 100% (48/48) | 100% (21/21) | 100% (21/21) | 6.6s | 27.9s | 4.5s |

**Tool calling still saturates — now two sizes lower.** The 2026-08-15 run
found no separation between 26B, 27B and 31B and suggested that was a ceiling
effect. It reaches down to 12B: right tool and right arguments on every run of
every case, including the three lifted from recorded incidents.

- **12b is 3.2x faster on the scheduled tasks** (4.5s vs 14.6s median). Chat
  medians are a wash — 26b is marginally quicker — but the **worst case is 4x
  better**, 27.9s against 119.1s. For a phone waiting on a reply the tail is
  what is felt, not the median.
- **12b did not reproduce 26b's `daily_synthesis` weakness**: 3 of 3 non-empty
  where 26b was 2 of 3, its one failure a `num_predict` cut-off with thinking
  on. Read this narrowly. `expect_count` is None for that case, so what was
  measured is that *something* came back.
- **One soft wobble on 12b, uncaught by any check.** Handed an event two days
  out it three times framed it as "in the next 24 hours" while stating the
  right date. 26b never did. Nothing scores an unprompted framing claim.

### The stale fixture that inverted the result

The first pass of this run said 26b failed `calendar_upcoming` 0 of 3 and
`notifications_sent` 0 of 3, and that 12b passed both. That was **the harness,
not the models.**

Both cases pinned a date: an event on `2026-08-17`, a push on `2026-08-14`.
Correct on 2026-08-15, when they were written. Three days later the "upcoming"
event was yesterday's and the "yesterday" push was four days old. The chat
system prompt bakes in the real current date, so the model saw the conflict —
and the scorer only checked that the reply contained `"team sync"`.

So the case rewarded the failure. 26b said "nothing is coming up", which was
right, and scored 0. 12b named the event and scored 3/3 while calling it
"today" in one run and "**tomorrow**, August 18" in another — the weekday
fabrication [model-constraints.md](model-constraints.md) exists to prevent,
scored as a pass.

**A rotted fixture does not fail. It inverts, quietly, in favour of whichever
model ignores dates.** Fixed by deriving every fixture date from today, adding
a wrong-day guard to `calendar_upcoming`, and a `pytest` check that fails if a
written-out date reappears. Re-measured at 5 reps: both models 5/5 on
`calendar_upcoming` and `calendar_weekday`, both naming August 20 correctly.
26b's one remaining loss is a degenerate reply — it answered the notifications
question with the fixture's own message field, `"Sent."`

### If the switch is made

It needs code that does not exist: `OLLAMA_MODEL` is a single global
(`agent/loop.py:175`). A per-task model would be `resolve_model(task_key)`
reading `WREN_<TASK>_MODEL`, symmetric to `resolve_backend`
(`agent/loop.py:339`), with the ~11 `complete_text()` call sites passing
`model=`. Measure the swap cost first: two resident models at `num_ctx=32768`
is roughly 12GB + 27GB of 48GB, with swap already in use.

## Run of 2026-08-24 — qwen3.8:27b-mlx vs gemma4:26b-mlx

180 runs, 3 reps, production settings. The MLX build the 2026-08-16 deferral was
waiting for. **Outcome: stayed on `gemma4:26b-mlx`** — and the run's real finding
was not about qwen at all, it was a prompt regression in the incumbent.

| | right tool | args right | used the result | non-empty task answer | median chat | p90 chat | median task |
|---|---|---|---|---|---|---|---|
| qwen3.8:27b-mlx | 98% (59/60) | 100% (24/24) | 98% (44/45) | 100% (21/21) | 9.6s | 51.1s | 9.0s |
| gemma4:26b-mlx | 95% (57/60) | 88% (21/24) | 98% (44/45) | 95% (20/21) | 4.1s | 14.0s | 2.5s |

**The MLX build removes the packaging penalty, not the family's latency.** The
GGUF arm ran 30-142s per case; this one medians 9.6s. That is in line with
`qwen3.6:27b-mlx`'s 15.7s and confirms the 2026-08-16 reading — most of the old
gap was packaging, the rest is the thinking-token cost, and the rest does not go
away. Chat p90 is 51.1s against 26b's 14.0s. On a phone the tail is what is felt.

**qwen3.8's two `calendar_colorizer` parse failures are ours, not the model's.**
It emitted correct JSON for all 12 events and then a stray `</think>` tag, and
`_parse_classification` died on the trailing text. Strip the tag and all three
reps parse. Read its task scores as 100%/100%, not 90%/89%. Nothing strips a
loose think tag today; that is a one-line fix in the parse path, still open.

### gemma4:26b stopped saving memories, and the prompt did it

`memory_remember` scored **0/3** for the incumbent — it replied "I've pinned that
preference for you" and called nothing. It had passed 3/3 on 2026-08-15 and
3/3 on 2026-08-18.

The cause was a paragraph added to `agent/wren_chat.md` on 2026-08-18 by
`073d699`, which exists to stop a confirmed write drawing a second confirmation
card:

> Once a tool result comes back, that action has already run. [...] Do not call
> the tool a second time to be sure

Every clause of that is true *after* a tool result. The block sat immediately
after "call the tool in that same reply", opened by asserting the action had
already run, and carried the strongest imperative in the section — *do not call
the tool*. With no tool result in the turn yet, the model applied it anyway and
reported a save it never made. This is the failure class
[model-constraints.md](model-constraints.md) already names — describing an action
instead of performing it — re-entering through the fix for a different bug.

**A prompt clause that is only true under a precondition must state the
precondition first, and restate the default after.** The repair scopes it
("if you can already see a tool result for that action in this conversation")
and closes by re-asserting what to do otherwise ("when no tool result for it is
there yet, the action has not run — make the call"). Measured 15 reps per arm,
interleaved, on the live model:

| | saves the memory | confirms once, no repeat card | handles a decline |
|---|---|---|---|
| old wording | 14/15 | 15/15 | 15/15 |
| scoped rewrite | 15/15 | 14/15 | 15/15 |

The rewrite's one `confirm_then_stop` loss is not the double-card bug returning:
it called the tool exactly once and `no_repeat_confirm_ok` passed — the reply
just said "The event has been added to your calendar" without naming the day or
title, so `final_ok` failed. A vague sentence, not a lost write.

### Three reps cannot tell 76% from broken

The bake-off's `0/3` looked like a hard break. It was a streak on a flaky one.
Re-measured across four batches, the old wording saved the memory **29 of 38
times (76%)**; every rewrite variant went 35/35.

| batch | old wording |
|---|---|
| bake-off (3 reps) | 0/3 |
| batch 1 | 4/5 |
| batch 2, interleaved | 11/15 |
| batch 3, interleaved | 14/15 |

Fisher one-sided p = 0.002 pooled, 0.026 on the interleaved batches alone. The
direction held in all four — the rewrite never lost — but the effect is a
*degradation*, not a break, and `--reps 3` cannot see the difference. **When a
case fails 0/3, re-measure it at 15 before believing the rate.** The default 3
reps is enough to flag a case, not to size it; sizing it is what separates "the
model cannot do this" from "we made it worse by a quarter".

The rate also drifted upward across batches (0%, 80%, 73%, 93%) with nothing
changed, which is its own reason to interleave arms rather than run them
back-to-back.

## Adding a case

Chat cases go in `evals/cases_chat.py`: a prompt, the expected tool (or `None`),
optional `arg_checks` predicates, canned `tool_results`, and optional
`final_must_contain` / `final_must_not_contain`. A test asserts the expected
tool is actually offered for that prompt under keyword pre-loading, so a case
can't quietly end up measuring `GROUP_KEYWORDS` instead of the model.

Task cases go in `evals/cases_tasks.py` and must import the task's real prompt
and real parser. A case with a hand-copied prompt goes stale the first time
someone edits the task. Each also carries a `golden` field — a hand-written
ideal answer, never sent to a model.

The golden answer is not decoration. **A miswired case and a bad model look
identical in the results table**, and the miswired one costs hours of Ollama
time to discover. Two guards close that gap, and both caught real bugs during
the build:

- every `arg_checks` key must be a parameter the tool's schema actually
  declares (a case checked `date` on a tool that takes `start`/`end`, so all
  three models scored 0 on it)
- every `golden` answer must parse to `expect_count` results (a parser was
  handed the *compacted* leads instead of the raw ones, dropping the id it keys
  on, so a flawless 10-of-10 answer scored as a parse failure)
- **no chat case may write a date out.** Every fixture date is derived from
  today (`_day`, `_at`, `_long` in `cases_chat.py`); `calendar_weekday` gets its
  day from production's own `resolve_date("next tuesday")`. An absolute date
  doesn't fail when it rots — it *inverts*, see 2026-08-18 below

All three run in `pytest`, in under a second.

## Safety

No eval run can touch production state. Tool dispatch is stubbed, so no Google,
Strava, mail or web call is ever made; confirmation-gated tools stop inside
`advance()` and execute nothing; results go to `evals/results/` (gitignored);
and each case's logger has `propagate=False`, so a two-hour run puts nothing in
`logs/wren.log`. `tests/conftest.py` redirects `RESULTS_DIR` to `tmp_path` so a
test's fixture records can't become the run `evals.score` reports on.

## Related

- [model-constraints.md](model-constraints.md) — the failures the cases encode
- [ollama-serving.md](ollama-serving.md) — the single-slot server this competes with
- [llm-backend.md](llm-backend.md) — the seam that makes the model swappable
