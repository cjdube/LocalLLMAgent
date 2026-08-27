# ClickUp — reading the backlog

Wren feature ideas had no home in the repo at all. That, not any shortcoming in
Google Tasks, is the gap ClickUp fills. The two stay side by side: **Google
Tasks holds dated chores** and feeds the morning brief; **ClickUp holds work
with shape** — ideas, bugs, features, and what has been parked.

This page covers step one, which is read-only. Wren can answer questions about
the backlog; she cannot change it.

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
.venv/bin/python -m agent.tools.clickup areas
```

## Areas

An **area** is a ClickUp Space, addressed by a slug of its name:

| Space | Area | Statuses |
| --- | --- | --- |
| Wren | `wren` | idea · designed · building · parked · **shipped** |
| Vibe Foundry | `vibefoundry` | to do · in progress · **complete** |
| Blog | `blog` | to do · in progress · **complete** |

Areas are **discovered on every call**, not pinned in config. The design started
the other way round — a `CLICKUP_AREAS=wren:<id>,…` map in `.env` — and
discovery replaced it once the live API turned out to answer the same question
in one GET. It costs about 200ms and buys three things: renaming or adding a
Space needs no edit and no restart, there is no id to hand-copy out of a browser
URL, and there is no config that can quietly disagree with reality.

The rule the pinned map existed to protect is intact either way: **the model
never sees a ClickUp id**. It says `area="wren"`, Python resolves the Space; it
says a *title*, Python resolves the task. Nothing opaque ever has to be copied
back ([docs/opaque-identifiers.md](opaque-identifiers.md)).

Two workspaces on one token is refused rather than resolved by picking the
first — that would silently answer about the wrong one.

## The two tools

### `list_backlog(area, status, include_done)`

Every argument is optional. Sorted most-recently-touched first. Each row is
title, area, status, tags, priority, and when it last moved.

The result always carries `areas`, so a model that does not know the area names
can call the tool bare and learn them from the answer — there is no way to be
stuck.

### `read_backlog_item(title)`

One item in full: description, comments, status, tags, priority, created and
updated dates, and the link to open it. Matching is forgiving — exact title
first, then a unique substring — because the model is passing back a title the
user said out loud. An ambiguous title returns the candidates rather than
picking one, the same posture as `read_project`.

It reads finished items too. The listing hides them; a finished item is still a
perfectly normal thing to ask about.

## The morning brief's Backlog section

`backlog_digest(since_ms)` is a **library function, not a chat tool** — no
`TOOL_SCHEMA`, deliberately. In chat the same question is answered better by
`list_backlog`, which can be asked follow-ups; the digest exists to give
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
`backlog_digest`, not after. A cursor stamped afterwards silently drops anything
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

## Three things that would each have been a silent bug

**Statuses are per-Space, and they differ.** `--status parked` against the Blog
space is not an empty backlog; it is a status that does not exist there. So a
status filter is validated against the chosen area's real statuses and the error
names them. Without that, the tool returns `[]` and the model reports that
nothing is parked.

**ClickUp hides its Closed group by default.** Not a rounding error: 21 of this
account's 57 items are shipped. `list_backlog` keeps ClickUp's default, because
"the backlog" means the live one — and says so in the result, so an omission
that large is never invisible. `include_done` turns it back on, and
`read_backlog_item` always sets it.

**Timestamps are UTC milliseconds; days are local.** ClickUp sends 13-digit
epoch milliseconds, sometimes as a string and sometimes as an int. They are
converted with `local_timezone()` from `agent/dates.py`, never sliced. The test
suite pins an evening timestamp, where the UTC day and the local day disagree —
the case a naive implementation passes anyway
([docs/timezones.md](timezones.md)).

## Size

A backlog row is about 120 characters, and 36 open items across three areas is
already 6,200 — against the loop's 8,000-character default cap, before a single
new idea is captured. So `list_backlog` carries its own character budget
(`_MAX_LIST_CHARS`), drops whole rows rather than slicing one, and says how many
of how many it is showing. A row cap would not have caught this: it bounds how
many rows come back, never how big they are.

`read_backlog_item` trims the description and the comments for the same reason,
and says how much it left out.

## What is deliberately not here

- **No `/backlog` dashboard page.** ClickUp's own interface is better than
  anything rendered here, and a page that re-displays it is duplicated work with
  a maintenance cost. Wren's value is connecting the backlog to what else she
  knows, not showing it back — which is what the morning brief's Backlog section
  does instead.
- **No polling and no scheduled task of its own.** The tools read on demand and
  the digest rides the morning brief's existing run. The only stored state is
  the one cursor in `config/clickup_state.json`.
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

## When writes arrive

Two findings settled before any write exists, recorded here so they are not
rediscovered:

**Free-text writes must be barred from unattended runs.** `read_backlog_item`
returns descriptions and comments, so any text a background job writes to
ClickUp is read back into a future Wren prompt. That is exactly the
durable, prompt-visible-state criterion that keeps `remember`, `pin` and
`write_skill` out of unattended runs. So `add_backlog_item` and
`comment_on_item` belong in `UNATTENDED_EXCLUDED_TOOLS`; `move_item` writes one
value from a fixed set and does not.

**A client guest changes what this text is.** Today the user is the only author,
so everything Wren reads here is his own words. The first guest invited into the
Vibe Foundry Space is the moment ClickUp joins the untrusted-input list
alongside web pages and email.
