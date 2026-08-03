# Writing code against a small local model

Wren's on-device model is small. Four failure modes have each cost weeks of
silent breakage, and each one produced a rule in
[CLAUDE.md](../CLAUDE.md)'s *Small-local-model constraints* section.

CLAUDE.md carries the rule and the measured result, because that's what's
needed while writing code. This file carries the evidence — the incidents, the
replay counts, the reasoning — because that's what's needed when someone is
about to decide the rule doesn't apply to their case. It usually does.

The four share a shape worth naming up front: **the failure is invisible.** In
every case the model returned something well-formed, the task exited 0, and no
alert fired. Nothing looked wrong until someone went looking.

## `think=False` for template-filling calls

**Rule:** pass `think=False` for any call that fills in a template — a
classification, a score, a fixed output format. Pass `logger=` too.

The model's thinking tokens come out of the same `num_predict` budget as its
answer. A call that reasons too long doesn't return a truncated answer — it
returns **empty content**, and the caller reports it as a parse failure. The
symptom points at parsing; the cause is budget.

**Incidents.** Twice: `calendar_colorizer` crashed on an empty response, and
`opportunity_digest` silently emailed 11 unscored leads.

**Measured**, on 40-lead scoring:

| | Runs producing output | Speed |
|---|---|---|
| thinking on | 0 of 3 | — |
| thinking off | 3 of 3 | 5x faster |

**Where thinking stays on:** only where the model must reason past what the
prompt already contains — `evaluate_app`, `daily_synthesis`. Measure even
there.

**"Analysis" is not the test.** This is the part that gets rationalized away.
`evaluate_against` writes a judgement, which sounds like reasoning — but it
judges a target against standards that are *both already in the prompt*. That's
comparison, not chain-of-thought. With thinking on, 1 run in 3 returned
nothing; turning it off also made the output cite the standards more
specifically. `research` fills a seven-label template from supplied snippets —
same story, same fix.

The test is not "is this analysis?" but **"must the model reason past what the
prompt already contains?"**

**Logging:** `agent/loop.py` warns when a generation hits the cap, but only if
it has a logger. A call without `logger=` fails silently by construction.

**On Gemini:** only the Ollama path acts on `think`. The equivalent knob is
`WREN_GEMINI_THINKING_BUDGET` (already 0 by default, and some models reject 0 —
see `agent/backends/gemini.py`).

## Degrading on bad model output must be logged

**Rule:** if a parse yields fewer results than inputs — not just zero — log it
at WARNING with the counts and the raw length.

"Degrade, don't crash" is right for a dead feed, whose absence is obvious in
the resulting digest. Applied to model output it hides the bug instead, because
a digest with 8 of 10 leads looks exactly like a digest with 10.

**Incidents**, all silent for weeks:

- a mis-copied event id matched no event, so that event was never coloured
- `_parse_scores` returned `{}` and the digest emailed 11 leads scored 0
- a batch scoring 8 of 10 lost two leads with no trace

A scheduled task that silently produces *less* is worse than one that fails,
because only the failure pushes an alert.

## Catalogue tools must deny pretraining

**Rule:** a tool that answers "what exists?" must say in its description that
the answer is **not something the model knows**, that only what the tool
returns exists, and what to say when it returns nothing.

A catalogue question is the one shape where pretraining supplies a *plausible*
answer, so the model skips the tool and invents one. General knowledge contains
a perfectly good list of games; it just isn't ours.

**Incident.** Asked "let's play a game" — a request to act, not a question
about what exists — the `list_games` tool went uncalled in **2 of 12 replays**,
and Wren offered Wordle, Sudoku and Chess with fabricated links.

**Fix and result.** A description that only says *when* to call the tool isn't
enough. Rewriting it to deny pretraining explicitly took the replay to **12 of
12**. The wording is pinned by a test in `tests/test_games.py`.

**Verify with the vague phrasing.** "What games are there?" calls the tool
regardless; "let's play a game" is what exposed the gap.

Applies to any future registry-style tool — games, installed apps, connected
devices.

## Never tell the model to describe an action it should perform

**Rule:** any instruction about a confirmation-gated tool must say *call the
tool in the same turn*, and that the pause is the app's job — not a reason to
wait for a verbal go-ahead.

**Incident.** `agent/wren_chat.md` used to say a write should be narrated
because "the app itself will pause … so just narrate your intent." Asked to put
a fetched Strava activity on the calendar, the model replied:

> I'll add "Evening Volleyball" … from 6:38 PM to 9:13 PM

…and emitted no `tool_call`. 2 of 3 replays; `logs/wren.log` 2026-08-01 06:06.

`advance()` correctly ends the turn on a reply with no `tool_calls` — so
nothing was written, nothing was gated, and nothing was logged. A text-only
reply is shaped exactly like a legitimate answer. The user only noticed by
asking twice.

**Fix and result.** Rewording `wren_chat.md` to require the call in the same
turn took the replay to **9 of 9**.

**Backstop.** `chat/server.py:_warn_if_promised_without_acting` — a final reply
that promises an action while the turn executed no tool now logs a WARNING
instead of vanishing.

## Related

- [CLAUDE.md](../CLAUDE.md) — the rules themselves, in short form
- [docs/llm-backend.md](llm-backend.md) — the backend seam and Gemini's knobs
- [docs/tool-loading.md](tool-loading.md) — how a tool reaches the model at all
