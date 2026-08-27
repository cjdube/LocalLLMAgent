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
  knows, not showing it back.
- **No polling, no watermark, no store, no scheduled task.** These tools read on
  demand. Change detection is only needed by a later step that reports *what
  moved since yesterday*; building it now would be a store, a `.gitignore` line,
  a conftest redirect and a plist earning nothing.
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
