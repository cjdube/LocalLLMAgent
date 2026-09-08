# Filing a Claude Code session as a ClickUp Task

`/clickup-ticket`, run from inside a Claude Code session, files that session as
a Task in the Wren Space: the plan's heading becomes the title, the first prompt
the user typed and the reply it drew become quotes in the description under
**Asked** and **Answered**, the plan `.md` is attached, and the Task moves to
`designed`.

The reply is there because the ask on its own is often a question — *can this be
done, and what would it take?* — and the answer is what the Task is really for.
A session with no prose reply yet is still filed; the **Answered** heading is
omitted entirely rather than left over an empty quote.

`designed` is not an arbitrary end state. It is the status
[`wren-build`](clickup-build.md) requires, along with exactly one attached `.md`
plan — so a Task filed this way is already in the shape that lets Claude Code
build it. Plan in a session, file it, tag it, and the plan never gets re-typed.

## Where the pieces live

| | |
|---|---|
| `~/.claude/skills/clickup-ticket/SKILL.md` | The skill. Finds the session id, runs the CLI, reads the result back. Outside the repo so it loads in any project. |
| `agent/session_ticket.py` | Everything else. Reads the transcript, builds the description, drives the three ClickUp calls. |
| `agent/tools/clickup.py` | `find_task_id` and `upload_attachment`, added for this. |
| `tests/test_session_ticket.py` | The suite. Fixtures are built from captured transcript line shapes. |

The split is deliberate: a skill file is prose and prose cannot be tested, so
the skill holds only the two facts it alone knows — which session this is, and
what the user asked for — and everything with a decision in it lives here under
pytest.

## Finding the session

Two anchors, either one enough.

**The session id.** It is also the name of Claude Code's scratchpad folder, so
the skill always has it without asking. The transcript is
`~/.claude/projects/<mangled cwd>/<session id>.jsonl`. The folder name is the
working directory with its separators replaced, and reversing that mangling is
lossy — a hyphen in a real folder name is indistinguishable from a slash — so
the id is globbed across every project folder instead of computed.

**The plan file.** Its basename is the session's `slug`, a field carried on
every line of that session's transcript. Checked against all 338 transcripts on
this machine: a slug belongs to exactly one session, both directions. So the
plan identifies its session with no index anywhere.

## Finding the first prompt

The first `type: "user"` line in a transcript is usually **not** a prompt. Real
first lines seen on this machine include an `isMeta` caveat block, a
`<command-name>/model</command-name>` wrapper, and a tool result. A reader that
takes the first user line quotes one of those onto the board.

The field that settles it is `origin`: `origin.kind == "human"` is set only on a
prompt a person actually submitted. Everything else — tool results, hook output,
injected reminders — has `origin: null`. Older terminal sessions carry
`promptSource: "typed"` instead, so the two are OR-ed.

Two traps behind that filter, both handled:

- `message.content` is a plain **string** for a typed prompt but an **array**
  when the prompt carried an image, and that array holds a megabyte of base64.
  Only `type: "text"` blocks are read out of it.
- Scheduled tasks are recorded with `origin.kind == "human"` too. They open with
  `<scheduled-task`, which is on the prefix skip-list with the command wrappers.

## Finding the first reply

One API response is **several transcript lines sharing a `requestId`**, one
content block each: `thinking`, then `text`, then a `tool_use` per call. So the
reply is the text blocks of the first `requestId` after the prompt, joined —
and a line with no text does not end it, because a text block often sits between
two tool calls. The next `requestId` is what ends it, which is how a later turn
in the same session stays out.

Thinking is skipped because it is never shown to the user, and `tool_use` is
skipped because it is a JSON payload, not prose.

The quotes only *look* like quotes because `add_clickup_task` sends the body as
ClickUp's `markdown_content` field. Sent as `description`, ClickUp stores the
text verbatim and the board shows the `>` and the `**` characters themselves
([docs/clickup.md](clickup.md)).

## Both quotes have to fit

ClickUp truncates an over-long description without saying so, and what it would
cut is the bottom — the provenance line, and the end of the answer. So both
quotes are cut here instead, each with a line saying how much was dropped:
2200 characters for the ask, 1000 for the reply, inside ClickUp's 4000.

The ask gets the larger share. The reply is context — what the session set off
to do — and its first paragraph carries nearly all of that, while the ask is
the thing the Task is about. `test_both_quotes_together_fit_inside_clickups_own_limit`
asserts the arithmetic rather than trusting it.

The title lines (`custom-title`, `ai-title`) are appended repeatedly as the
title is re-asserted, so the last one wins and a hand-set title beats the
generated one. Those lines carry only `type` and `sessionId` — no `uuid`, no
`timestamp` — so nothing in the reader may assume a line is shaped like a
message.

Timestamps in a transcript are UTC with a `Z`; the date on the Task is local,
converted through `local_timezone()`, never sliced ([docs/timezones.md](timezones.md)).

## Create, attach, then move

```
read_clickup_task   is it already filed?   -> refuse, do not duplicate
add_clickup_task    title, quote, priority
find_task_id        the id, which the create deliberately did not return
upload_attachment   the plan .md
move_clickup_task   -> designed
```

**The move is last** so the board never shows `designed` on a Task whose plan is
still missing — that is precisely the state `wren-build` refuses.

**Everything after the create can fail on its own, and the Task already
exists by then.** Those failures come back as `warnings` on a successful result,
never as an error. An error would read as "nothing happened", and the user would
run it again and file a second Task.

The duplicate check has a sharp edge worth naming: `read_clickup_task` reports
"no such task" as an `error`, so a missing Task and an unreachable ClickUp
arrive in the same shape. They are told apart on the message text, because
guessing the wrong way files a duplicate.

## Why two new library functions, and why neither is a chat tool

`add_clickup_task` deliberately does not return the id of what it created — its
result goes to the model, and an id in the model's context is one it will
eventually be asked to copy back ([docs/opaque-identifiers.md](opaque-identifiers.md)).
So `find_task_id` exists for callers that have just created a Task by a title
they chose themselves. It shares `_find_task` with every write, so it can never
land on a different Task than the write that preceded it.

`upload_attachment` is the **third HTTP door** in `agent/tools/clickup.py`.
`_write` hardcodes a JSON content type and a `json=` body; ClickUp's attachment
endpoint is multipart, and `requests` must set the boundary itself. That is why
it cannot be routed through `_write`, and why it is named in its own right in
`tests/conftest.py:_block_clickup_egress` — a new door not named there reaches
the live API from a green test run.

Both take an id, so neither has a `TOOL_SCHEMA`, and neither appears in
`agent/toolset.py` or `chat/insights.py`. Same class as `tagged_clickup_tasks`
and `remove_clickup_tag`.

## Limits, on purpose

- **The Wren Space only**, by default. It is the Space whose workflow defines
  `designed`. `--space` overrides it, but `--status` then needs to name a status
  that Space actually defines.
- **One plan, and it must exist.** No plan file means refused, not filed.
- **No re-filing.** A second run refuses rather than updating the existing Task;
  `--force` files another one.
- **The quote is cut out loud.** ClickUp truncates an over-long description
  silently, which would leave a quote ending mid-word with nothing to explain
  it, so it is cut here instead with a line saying how much was dropped.
- **The whole path is model-free.** The title is read off the plan, the quote is
  the user's own words, the date is arithmetic.
