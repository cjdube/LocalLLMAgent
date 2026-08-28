# Daily learnings — Chrome and YouTube

Two tasks, one shape: read yesterday's activity from one channel, have the local
model draft a short review of what it taught, and write one Markdown file per
day into the Obsidian vault (`LEARNINGS_DIR`). They share the gather → persist →
email helpers in `agent/activity_log.py`.

A third task, [ai_chat_learnings](ai-chat-learnings.md), covers the same ground
for AI-agent conversations and has its own doc because its sources are unusual.

| Task | Schedule | Channel |
|---|---|---|
| `scribejay/daily_chrome_learnings.py` | Daily 5:15 AM | Chrome browsing history |
| `scribejay/daily_youtube_learnings.py` | Daily 5:05 AM | YouTube Liked videos |

## Chrome

Reads the prior day's history via `agent/tools/chrome_history.py` and asks the
model for a compact daily log in two sections:

- **Tools & Tech Encountered**
- **Product & Strategy** — for product-management reading

Written as `Daily-Chrome-<date>.md`.

**Each site carries its top page *paths*, not just the domain.** This is the
detail that makes the review say what was being looked into rather than restate
a tab title. A domain alone ("github.com") tells the model nothing; the paths do.

**Exclusions.** Sites and pages matching `learnings.excluded_domains` or
`learnings.excluded_keywords` are skipped — see [preferences](preferences.md).

**Known gap:** `NOISE_DOMAINS` drops `google.com`, so search-mediated research
never reaches the log. If a day's review looks thin, check the unfiltered
history before blaming the prompt.

## YouTube

Reads the prior day's Liked videos via `agent/tools/youtube.py` — title,
channel and description, **not** transcripts. The model writes a short synthesis
of what they teach; the list of the exact videos (verbatim titles, scheme-validated
URLs) is appended **in Python**, not by the model, so the links are always real.
Written as `Daily-YouTube-<date>.md`.

Likes are timestamped UTC and the day window is local — `_liked_local_date`
converts before comparing. See the day-boundary rule in [AGENTS.md](../AGENTS.md).

## Shared behavior

**A quiet day writes nothing.** No meaningful browsing, no Likes, or a draft
that amounts to "None" produces no file. This is normal, not a failure.

**A failed vault write emails the draft instead**, and pushes a phone alert —
e.g. when `LEARNINGS_DIR` points somewhere that doesn't exist. The draft cost a
model call; it's never silently dropped.

**The prompt is deliberately small and focused** so the on-device model produces
a complete draft rather than trailing off. See the small-local-model constraints
in [AGENTS.md](../AGENTS.md) and [docs/model-constraints.md](model-constraints.md).

## Where the output goes

`LEARNINGS_DIR` is the vault's `raw/` — a write-only drop that ObsidianWikiAgent
ingests and summarizes into the `wiki/` concept pages Wren later reads via
`agent/tools/wiki.py`. Anything written there becomes an asserted wiki page, so
only reviews go here; questions and nudges go to `SYNTHESIS_DIR` instead (see
[docs/daily-synthesis.md](daily-synthesis.md)).

## Related

- [docs/ai-chat-learnings.md](ai-chat-learnings.md) — the third learnings task
- [docs/daily-synthesis.md](daily-synthesis.md) — consumes these files the next morning
- [docs/preferences.md](preferences.md) — the exclusion keys
