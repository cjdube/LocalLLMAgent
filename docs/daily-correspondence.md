# Daily correspondence — who got written to

`scribejay/daily_correspondence.py`, daily at 5:20 AM. Reads yesterday's Gmail
SENT **metadata** — headers, never bodies — groups it by conversation and writes
`Correspondence-<date>.md` into `CORRESPONDENCE_DIR`.

**Why it exists.** The record already covers what was built
([daily-commits.md](daily-commits.md)), what was read
([daily-learnings.md](daily-learnings.md)) and where the hours went (Claude Code
time blocks). None of it says who the day was spent talking to. Sent mail is the
only source that does, and `gmail.readonly` is already consented, so it costs no
new OAuth.

## What it reads

`agent.tools.gmail_read.fetch_sent_metadata` — a library function, not a chat
tool, the same shape `get_events_in_range` has for the calendar colorizer. Wren
has no registered tool for this: reading the record of who was written to is
journaling, and Wren reads the record rather than the mailbox.

Each row is `{message_id, thread_id, to, cc, subject, date, is_reply}` and
nothing else. Gmail is asked with `format="metadata"`, so a body is never
*fetched* — not fetched and then discarded. The test asserts that on the request
arguments, because a row with no body would look identical either way.

**The day window is sent as epoch seconds.** Gmail's `after:`/`before:` take whole
days in the *account's* timezone, which is not necessarily the machine's, and
would quietly slice the day wrong ([timezones.md](timezones.md)).

## No model call

Deliberate, and a deviation worth naming. With no bodies the model would have a
subject line and some names, and any sentence richer than those is invented. The
subject the user wrote is already the most accurate description of the exchange
that exists, so the page is assembled in Python — the way
`scribejay/strava_download.py` maps Strava fields onto calendar events with no
natural-language step.

## The noise filter

Sent mail is not all correspondence. In a sample fortnight, 4 of 10 sent messages
were Wren writing to the user.

| Rule | Catches |
|---|---|
| Every recipient is the user himself | Morning Brief, opportunity digests, log-inspector rollups |
| Subject is in `NOISE_SUBJECTS` | An `unsubscribe` click, which really is sent mail |

The self-addressed rule is the principled one and comes first. `NOISE_SUBJECTS`
is a short list expected to grow; a missed entry costs one junk line in a page,
not a failed run.

A rejected third rule: *"the recipient is not a real address."* Real data
disproved it — the unsubscribe went to
`32.MRTVIML…@unsubscribe2.customer.io`, which is a perfectly valid address.

## The page

Two sections, from the **first** message of the day on each thread:

- `### Reached out` — a thread he started. Deciding to start something is the
  part of a day worth recording.
- `### Replied` — answering, which is routine.

One line per conversation, not per message: `**Subject** — Name, Name (3
messages)`. Threads are grouped on Gmail's own thread id, and the `Re:` prefix is
stripped because the section already says it was a reply.

**People are keyed on address, not on display name.** The same person arrives
named on one message and bare on the next — Diego was on one real thread as both
`Diego M. Oppenheimer` and `diego@hq.aitinkerers.org`, and a set of strings listed
him twice. A real name beats a bare address whichever order the two arrive in.

## Where it writes, and why not the vault

`CORRESPONDENCE_DIR`, default `~/Vaults/llm-wiki-learnings/correspondence`.

**Not `LEARNINGS_DIR`.** That is the vault's `raw/`, ObsidianWikiAgent's ingest
queue, and anything dropped there becomes an asserted wiki page — which would turn
the people he emails and the companies they work for into wiki entities. A record
of who he wrote to is a diary. `daily_synthesis` keeps its nudges out of the queue
for the same reason ([daily-synthesis.md](daily-synthesis.md)).

## Behavior

**A day with no qualifying mail writes nothing.** Normal on a weekend.

**Gmail failing logs a WARNING and the run still succeeds.** A dead source reads
as an empty day to its caller, but never silently — that line is what separates a
missing page caused by a broken API from one caused by a quiet day.

**Not knowing the mailbox owner's address is a hard failure.** Every rule on the
page is "everyone who is not him"; with no address the whole day looks like mail
to strangers, which is worse than writing nothing. Gmail is not even queried until
the identity resolves.

## Running it by hand

```bash
.venv/bin/python -m scribejay.daily_correspondence
```

`--date 2026-08-21` writes one named day; `--backfill 14` writes each of the last
14 days, oldest first. A backfill is **one run** in the dashboard's history, not
N ([logs.md](logs.md)), and one day whose fetch fails does not stop the rest.

Re-running a day overwrites that day's file, so a backfill is safe to repeat.

## Related

- [docs/scribejay.md](scribejay.md) — the agent this belongs to
- [docs/daily-commits.md](daily-commits.md) — the building half of the same daily record
