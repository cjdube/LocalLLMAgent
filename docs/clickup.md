# ClickUp — Spaces, Lists and Tasks

Wren feature ideas had no home in the repo at all. That, not any shortcoming in
Google Tasks, is the gap ClickUp fills. The two stay side by side: **Google
Tasks holds dated chores** and feeds the morning brief; **ClickUp holds work
with shape** — ideas, bugs, features, and what has been parked.

**Three nouns, used exactly as ClickUp uses them.** A **Space** is a top-level
area of the workspace; a **List** sits inside a Space; a **Task** sits on a
List. Nothing in this module assumes any of them is called anything in
particular — every name is read from the account on the call that needs it, and
the tools work on any List in any Space.

Every tool is named `*_clickup_*`. Google Tasks owns the bare word *task* and
its tools are always loaded, so the ClickUp name is what keeps the two apart in
the model's own tool list; the keyword gate is only the pre-loader. Saying
"ClickUp" in chat is the reliable way in.

## Setup

One key in `config/.env`:

```
CLICKUP_API_TOKEN=pk_…
```

ClickUp avatar → **Settings** → **Apps** → *API Token*. It is a personal token,
it never expires, and it is sent as a **raw** `Authorization` header — no
`Bearer` prefix, which is what an OAuth app token uses and what returns 401 if
you mix them up.

Check it without the chat server:

```bash
.venv/bin/python -m agent.tools.clickup spaces
```

## Spaces, Lists and Tasks

A Space and a List are both addressed by a slug of their **name**, never by id.
The account as it stands:

| Space | Say | Lists in it | Statuses |
| --- | --- | --- | --- |
| Wren | `wren` | Backlog | idea · designed · building · parked · **shipped** |
| Vibe Foundry | `vibefoundry` | List | to do · in progress · **complete** |
| Blog | `blog` | Blog Planner | to do · in progress · **complete** |

Each Space happens to hold one List today. **Nothing depends on that.** A List
name narrows any read, and a Space that grows a second List makes the name
required on a write rather than breaking — see *Ambiguity is a question* below.

A **Folder** is ClickUp's organising layer between a Space and a List. Tasks do
not live on Folders, so this module does not model that level: `_lists` reads
the Lists sitting directly on a Space *and* the ones inside its Folders, and
flattens them into one set.

Spaces and Lists are **discovered on every call**, not pinned in config. The design started
the other way round — a `CLICKUP_AREAS=wren:<id>,…` map in `.env` — and
discovery replaced it once the live API turned out to answer the same question
in one GET. It costs about 200ms and buys three things: renaming or adding a
Space needs no edit and no restart, there is no id to hand-copy out of a browser
URL, and there is no config that can quietly disagree with reality.

The rule the pinned map existed to protect is intact either way: **the model
never sees a ClickUp id**. It says `space="wren"`, Python resolves the Space; it
says a *title*, Python resolves the Task. Nothing opaque ever has to be copied
back ([docs/opaque-identifiers.md](opaque-identifiers.md)).

### Ambiguity is a question, never a default

Four places could quietly guess, and none of them do. Each refuses and names
the options instead:

| Ambiguity | What happens |
| --- | --- |
| The token sees two workspaces | Refused — it would silently answer about the wrong one |
| A Space holds several Lists and no List was named | Refused, naming the Lists |
| A title matches two Tasks | Refused, returning the candidates |
| A List is named with no Space | Refused — two Spaces may hold Lists of the same name |

The last one is the reason `list_name` requires `space`. Searching every Space
for a List called "Backlog" has no correct answer the moment a second Space has
one, and the wrong answer looks exactly like the right one.

## The read tools

### `list_clickup_spaces()`

Every Space, the Lists inside it, and the statuses it defines. What the model
calls to learn the names before it uses one, and what answers "why did my
status filter not match?" on its own.

It is the only read that fetches Lists — that is a GET per Space, and the
ordinary listing must not pay it.

### `list_clickup_tasks(space, list_name, status, include_done)`

Every argument is optional. Sorted most-recently-touched first. Each row is
title, **the Space and List it sits on**, status, tags, priority, and when it
last moved. The List comes back on every task from ClickUp already, so naming
it costs no extra call — and without it a Space with several Lists reads back
as one undifferentiated pile.

`list_name` narrows to one List and is applied by ClickUp server-side
(`list_ids[]`), not filtered locally. It requires `space`.

The result always carries `spaces`, so a model that does not know the space names
can call the tool bare and learn them from the answer — there is no way to be
stuck.

### `read_clickup_task(title)`

One item in full: description, comments, status, tags, priority, created and
updated dates, and the link to open it. Matching is forgiving — exact title
first, then a unique substring — because the model is passing back a title the
user said out loud. An ambiguous title returns the candidates rather than
picking one, the same posture as `read_project`.

It reads finished items too. The listing hides them; a finished item is still a
perfectly normal thing to ask about.

## The morning brief's Backlog section

`clickup_digest(since_ms)` is a **library function, not a chat tool** — no
`TOOL_SCHEMA`, deliberately. In chat the same question is answered better by
`list_clickup_tasks`, which can be asked follow-ups; the digest exists to give
`tasks/morning_brief.py` everything it needs in one call.

It returns two lists, stacked in one section, because each is thin on its own:

- **`moved`** — what changed since the cursor. This is the news, and it is
  silent on a quiet day. It includes finished items, because "X shipped" is the
  most interesting thing that can happen to a backlog item and ClickUp's default
  filter would drop exactly that.
- **`in_flight`** — what is in ClickUp's **Active** status group right now,
  freshest first. This reads the same every morning until something changes,
  which is the point: it is the standing answer to "what am I in the middle
  of?".

Plus **`stalest`**: the in-flight item untouched longest. It is taken from the
tail of the sort *before* the cap, so it can name something the visible list
never showed — which is exactly where a stalled item hides. It is suppressed
below `_STALE_DAYS` (7) and for a single-item list, so the line only appears
when it means something rather than nagging about last Friday's work.

**Two fetches, not one filtered locally.** The moved window needs closed items
and the in-flight list must not have them, so they cannot share a call.

**Active is read off the Space, never off the task.** The status *group* comes
from each Space's own status table (`open` = Not Started, `custom` = Active,
`closed` = Done). Matching on status *names* would need an edit here every time
a Space's workflow changes — and the Wren Space (idea/designed/building/parked/
shipped) and the others (to do/in progress/complete) already disagree.

**The cursor is stamped before the fetch.** `checked_ms` is taken at the top of
`clickup_digest`, not after. A cursor stamped afterwards silently drops anything
changed while the brief was running — never seen again, and no error. It is
persisted to `config/clickup_state.json` only after the email actually sends,
and **independently of the starred-repo cursor**: a GitHub outage must not skip a
day of ClickUp activity, or the other way round. The first ever run has no
state, so it looks back 24 hours rather than reporting the whole backlog's
history as "moved yesterday".

**No model call.** The lists are facts and the labels are written in Python, so
a quiet day produces a short section rather than a paraphrase of nothing. The
caps (`_MAX_MOVED` 8, `_MAX_IN_FLIGHT` 6) are readability caps for a human
reading over coffee, not context budgets — and the "+N more" line is counted off
the true total, so a capped list never reads as the whole backlog.

## The write tools

Three, all in `WRITE_TOOLS`, so each one pauses for a tap on the phone before it
runs. Nothing below happens without that tap.

### `add_clickup_task(title, space, list_name, description, tags, priority)`

`space` is **required**. There is no safe default, and a Task filed in the wrong
Space is worse than a question — it is invisible exactly where the user goes
looking for it.

`list_name` is optional **only because a Space holding a single List names that
List unambiguously**. A Space holding several and no name given is refused, not
defaulted to the first.

**The status is not a parameter.** A new item opens at the Space's own first
Not-Started status, read off the Space: `idea` in the Wren Space, `to do` in the
others. So the model never picks a status on create, and never has to know the
Spaces disagree on the word.

**Priority is a word in, a number out.** ClickUp's priority field is inverted and
numeric (1 = Urgent, 4 = Low). The schema takes `urgent|high|normal|low` and
Python maps it. Never make the model emit the digit.

### `move_clickup_task(title, status)`

The status is validated against the statuses **that item's own Space** defines,
not against a merged list of every Space's. `parked` exists in the Wren Space and
nowhere else; validated against the merge, a move of a Blog item to `parked`
either fails with a 400 or lands somewhere nobody asked for.

Moving an item to the status it is already in **writes nothing** and says so,
rather than reporting a move that did not happen.

### `comment_on_clickup_task(title, comment)`

Where findings, links and decisions go, so an item carries its own history.
Chat history is in-memory and lost on restart; the item is not. Posted with
`notify_all: false` — this is Wren writing to the user's own backlog, not
something to page a workspace about.

### One matcher, shared with the read

All three resolve a title through the same `_find_task` helper that
`read_clickup_task` uses: exact match first, then a unique substring, and an
ambiguous title returns the candidates rather than picking one. A write that
resolved a title *differently* from the read the user just did is the trap
nobody tests for. On a read an ambiguous title costs a question; on a write it
would change the wrong item, and nothing would tell the user to look.

### What is gated, and what is barred outright

| Tool | `WRITE_TOOLS` (tap) | `UNATTENDED_EXCLUDED_TOOLS` (barred from background runs) |
|---|---|---|
| `add_clickup_task` | yes | **yes** |
| `comment_on_clickup_task` | yes | **yes** |
| `move_clickup_task` | yes | no |

The two that write **free text** are barred from unattended runs, because
`read_clickup_task` renders a description and its comments straight into a later
Wren prompt. Text a background job writes to ClickUp is therefore durable,
prompt-visible state the model authored itself — the same criterion that keeps
`remember`, `pin` and `write_skill` out of background runs.

`move_clickup_task` sets one value out of a fixed list the Space defines. There is no
text, so there is nothing to plant. It stays approval-gated like every other
write.

Each result carries `tool_name` and says what it wrote. A gated call returns out
of the tool-calling loop, so the iteration budget resets on every continuation —
naming the tool in its own result is what stops one request producing four cards
([docs/limits.md](limits.md)).

## The tag watcher

Put `wren-research` or `wren-context` on any Task and Wren answers it. The answer
arrives as a comment on that Task. `tasks/clickup_watcher.py` polls every five
minutes; nothing tagged means nothing happens and nothing is logged.

| Tag | What she does | What she must not do |
| --- | --- | --- |
| `wren-research` | Searches the web, fetches the two or three most useful pages, comments what she found with the URL for each point | — |
| `wren-context` | Searches your wiki and reads the pages it finds, comments what your notes already say and names each page | Search the web |

They are ordinary ClickUp tags. Type one onto a Task once and it exists.

### Three stages, and why they are three

**Notice** — `tasks/clickup_watcher.py`. One `GET /team/{id}/task` with the tag
filter. **It never calls the model.** Ollama serves one request at a time and a
queued request cannot be cancelled ([docs/model-constraints.md](model-constraints.md)),
so a poller that called the model would silently starve chat every five minutes,
and the symptom would be "Wren is slow this afternoon", not "the watcher is
wrong". Several `tags[]` values are OR-ed by ClickUp — verified against the live
workspace — so this is one call however many tags are watched.

**Decide** — there is no decide stage, on purpose. Compare
`tasks/_mail_action.py`, which needs a whole tool-free classify step because an
email arrives with no instruction attached. Here **the tag is the decision**: the
user chose `wren-research` over `wren-context` with his own hands, and a model
asked to re-derive that choice can only get it wrong. Python fills in a per-tag
template with the Task's title and description and hands the text to
`background.start_job(..., origin="clickup")`.

**Act** — `tasks/bg_worker.py`, the one place already allowed to hold the model
slot for a long time. It runs the job, pauses on the comment, pushes it to the
phone, and writes it once the tap comes back.

### Every comment starts with the tag that asked for it

`wren-research: …` or `wren-context: …`. The comment goes into ClickUp under the
user's own name — it is his personal token — and by the time it lands the tag is
already off the Task, so without the prefix there is nothing to tell his own note
from Wren's answer.

It is done **twice on purpose**. The job prompt asks the model to start with it,
so the approval card on the phone shows the line that will actually appear. Then
`tasks/bg_worker.py` stamps it on anyway, case-insensitively and only when it is
missing. A prefix the model was merely asked for is a prefix the model can drop,
and the whole point is that it is always there.

The prefix rides on the job (`background.start_job(..., comment_prefix=...)`), so
it survives the store and is still correct on the poll *after* the approval — a
different job record by then, rewritten twice. Chat and mail jobs pass nothing
and their comments are untouched.

### Why the tag comes off first

Removing the tag is what stops the same Task being picked up twice, and it
happens **before** the job is queued, not after.

The other order looks safer and is not. If the removal fails, a queue-first
watcher has both queued the job *and* left the tag on, so it queues that same
Task again on the next poll, and on every poll after that — each one taking the
model slot. Tag-first, a crash in the gap loses one request: no comment appears,
which the user can see, and re-tagging is one click. **A lost request is cheaper
than a loop that spends the model slot**, so that is the trade taken.

### What the origin buys

`origin="clickup"` is provenance, not policy — the policy is two functions in
`agent/toolset.py`, and the daemon passes the origin and never a tool list.

- `confirm_set_for("clickup")` returns `CONSEQUENTIAL_TOOLS | WRITE_TOOLS`:
  **every write pauses.** The user tagged a Task and walked away, so nothing he
  has not read may be written. In practice that is the one comment.
- `excluded_for("clickup")` hands `comment_on_clickup_task` back, which every
  other origin is denied outright.

That carve-out is the one judgement call in this feature, so it is worth being
explicit about. `comment_on_clickup_task` is excluded from background runs
because `read_clickup_task` renders comments into a later prompt, so text a job
wrote unattended becomes prompt-visible state it authored itself. That reason
still holds here. It is answered differently rather than waived: **excluded**
means the model may not do this; **gated** means the user does it and the model
drafts it. A tagged Task exists to get a comment, so denying the tool would make
the feature silently do nothing — and gating it puts the text in front of the
user before it lands. `add_clickup_task` is *not* handed back: a tagged Task asks
for an answer on itself, and a job that can create Tasks can grow the workspace
while nobody is looking.

### A dead network is one push, not 288

`notify_failure` does not dedupe, and at a five-minute interval a push per failed
poll is roughly 288 phone alerts a day for one dead router. So the watcher counts
consecutive failures in `config/clickup_watcher_state.json` and pushes once, when
the count reaches 10 — about fifty minutes. A blip is invisible; a real outage is
one alert. The count resets on the first success, which is logged too, so the log
says when it came back.

A failed poll exits 0. A poller that cannot reach a third-party service is not a
broken poller, and launchd is the wrong place to say so.

The watcher is a polling job (`StartInterval`, no `StartCalendarInterval`), so it
is excluded from the `/map` dashboard's run history on purpose and needs no
`Starting …` / `… run complete` log lines.

### The two library functions

`tagged_clickup_tasks()` and `remove_clickup_tag()` live in
`agent/tools/clickup.py` but are **not** chat tools and have no schema. They are
the only functions in that module that take a ClickUp **id**, which is exactly
why: an id must never reach the model
([docs/opaque-identifiers.md](opaque-identifiers.md)). Everything the model can
call takes a title.

`tagged_clickup_tasks` sets `include_closed`: a tag on a shipped Task is still a
request.

### The tag name may not contain a slash

The first spelling was `wren/research`, and it shipped broken. ClickUp's **router**
rejects a slash in this path — `%2F` or raw — with a plain-text `404 page not
found` that never reaches their code. Measured live on 2026-08-27:

| Tag name | `DELETE /task/{id}/tag/{name}` |
| --- | --- |
| `zzznotatag` | 200 |
| `wren%2Fresearch` | 404 |
| `wren/research` | 404 |
| `wren-research`, `wren_research`, `wren:research`, `wren.research` | 200 |

A slash cannot live inside one path segment, so encoding is not a workaround, and
there is no other endpoint — ClickUp has no "set all tags" call. A slashed tag is
therefore **unremovable**, which meant the watcher warned every five minutes and
queued nothing, forever. Two guards now: `remove_clickup_tag` refuses a slashed
name with a message that says why, and a test asserts no watched tag has one.

## The backlog as synthesis anchors

`backlog_anchors()` is the third library function here, alongside `clickup_digest`
and `tagged_clickup_tasks`, and like them it has no `TOOL_SCHEMA` — in chat the same
question is answered better by `list_clickup_tasks`, which can be asked follow-ups.

Its one caller is `tasks/daily_synthesis.py`, which matches yesterday's browsing
against the things he parked: *"you read about columnar storage last night, that is
the idea you wrote down three weeks ago."* It returns title, description and tags for
every open item that has a description, plus `skipped` — the count of items that
have none.

Two facts shape it. **ClickUp returns a task's description on the list endpoint**, so
the whole backlog costs one request; reading each item individually would be one call
per item against a 100/minute budget to learn nothing extra. And **an item with no
description is not an anchor** — 25 of the 37 open items were bare when this shipped,
and each would have been a two-token anchor outranking the entire vault while saying
nothing. Why the count is returned rather than swallowed is in
[daily-synthesis.md](daily-synthesis.md#bare-titles-are-not-anchors).

Done items are excluded. A shipped idea is not something to be nudged toward.

## Three things that would each have been a silent bug

**Statuses are per-Space, and they differ.** `--status parked` against the Blog
Space is not an empty result; it is a status that does not exist there. So a
status filter is validated against the chosen Space's real statuses and the error
names them. Without that, the tool returns `[]` and the model reports that
nothing is parked.

**ClickUp hides its Closed group by default.** Not a rounding error: 21 of this
account's 57 items are shipped. `list_clickup_tasks` keeps ClickUp's default, because
"the backlog" means the live one — and says so in the result, so an omission
that large is never invisible. `include_done` turns it back on, and
`read_clickup_task` always sets it.

**Timestamps are UTC milliseconds; days are local.** ClickUp sends 13-digit
epoch milliseconds, sometimes as a string and sometimes as an int. They are
converted with `local_timezone()` from `agent/dates.py`, never sliced. The test
suite pins an evening timestamp, where the UTC day and the local day disagree —
the case a naive implementation passes anyway
([docs/timezones.md](timezones.md)).

## Size

A task row is about 120 characters, and 36 open Tasks across three Spaces is
already 6,200 — against the loop's 8,000-character default cap, before a single
new idea is captured. So `list_clickup_tasks` carries its own character budget
(`_MAX_LIST_CHARS`), drops whole rows rather than slicing one, and says how many
of how many it is showing. A row cap would not have caught this: it bounds how
many rows come back, never how big they are.

`read_clickup_task` trims the description and the comments for the same reason,
and says how much it left out.

## What is deliberately not here

- **No `/backlog` dashboard page.** ClickUp's own interface is better than
  anything rendered here, and a page that re-displays it is duplicated work with
  a maintenance cost. Wren's value is connecting the backlog to what else she
  knows, not showing it back — which is what the morning brief's Backlog section
  does instead.
- **No scheduled task for the *reads*.** The tools read on demand and the digest
  rides the morning brief's existing run; the only state that costs is the one
  cursor in `config/clickup_state.json`. The tag watcher above does poll, but it
  polls for a *request the user made* rather than for news, and it costs one GET
  every five minutes and no model time at all.
- **No webhooks.** They need a public HTTPS endpoint; Wren binds `127.0.0.1`
  behind `tailscale serve`.
- **No ClickUp MCP server.** It is OAuth-only and capped at 50 calls per 24
  hours on the free plan without their AI add-on. The plain REST v2 API with a
  personal token is 100 requests per *minute*. Same lesson as the Composio →
  Strava-direct move.
- **No custom fields.** The free plan caps them at roughly 60–100 uses a month
  and every ClickUp guide tells you to build on them. Tags and the built-in
  priority field carry the labelling instead. (Priority is inverted in the API:
  1 = Urgent, 4 = Low.)

## Settled before the writes were built

Both of these were decided in the design and are now implemented above,
recorded here so the reasoning is not rediscovered:

**Free-text writes must be barred from unattended runs.** `read_clickup_task`
returns descriptions and comments, so any text a background job writes to
ClickUp is read back into a future Wren prompt. That is exactly the
durable, prompt-visible-state criterion that keeps `remember`, `pin` and
`write_skill` out of unattended runs. So `add_clickup_task` and
`comment_on_clickup_task` belong in `UNATTENDED_EXCLUDED_TOOLS`; `move_clickup_task` writes one
value from a fixed set and does not.

**A client guest changes what this text is.** Today the user is the only author,
so everything Wren reads here is his own words. The first guest invited into the
Vibe Foundry Space is the moment ClickUp joins the untrusted-input list
alongside web pages and email.
