# The opportunity scout — how it works

The fractional-work opportunity scout finds leads for Vibe Foundry from free,
ToS-clean sources, has the local model score them, and emails a daily digest.
This page explains the full lifecycle: when it runs, what it looks at, how the
list is built, what triage does, and how deduping and scoring behave over
time.

Code: `tasks/opportunity_digest.py` (pollers, scoring, digest),
`agent/tools/opportunities.py` (the store and chat tools),
`agent/tools/research.py` (company research briefs),
`chat/static/opportunities.html` + routes in `chat/server.py` (the triage
page). It deliberately does NOT scrape LinkedIn or use paid data SaaS — see
`CLAUDE.md`'s data sourcing policy.

## When does it run?

Every morning at 7:30 via launchd
(`launchd/com.craigdube.localllmagent.opportunitydigest.plist`). You can also
ask Wren in chat to "send the opportunity digest" anytime — the chat tool
(`send_opportunity_digest`) runs the exact same pipeline
(`build_and_send_digest()`), behind a tap-to-confirm since it sends an email.

## What is it looking at?

Three sources, each polled fresh every run. Each degrades to empty on error —
one dead feed never kills the digest.

1. **SEC EDGAR Form D filings** ("just funded") from companies headquartered
   in New England (`OPP_STATES`, default `MA,NH,ME,VT,RI,CT`). Only filings
   since the last successful run are requested, tracked by a watermark date in
   `config/opportunities_state.json`. Fund/trust/SPV/series-LLC paperwork is
   filtered out by name (`_FUND_NAME_RE`), and multiple same-day filings by
   one filer collapse into a single entry with an "(N filings)" count.
2. **The ATS watchlist** — the public job boards (Greenhouse/Lever/Ashby) of
   companies added to the watchlist, filtered to product/eng leadership titles
   (VP/Head/Director/CPO/CTO of product or engineering). The watchlist starts
   empty; curate it in chat ("watch Acme on greenhouse as acme") or on the
   `/opportunities` page.
3. **HN "Who is hiring"** — the current month's thread, keyword-filtered to
   leadership/fractional-flavored top-level posts.

## How the list is built

Every polled result gets a stable natural id (EDGAR filer + date, ATS job id,
HN comment id) and is inserted into `config/opportunities.json` — but only if
that id isn't already there. Brand-new items enter with status `new`.

Then the stall check runs: any watched leadership opening still open past
`OPP_STALLED_DAYS` (default 45) is re-flagged from `hiring` to
`stalled_search` and put back into `new`. This happens exactly once per
opening — that's the "stalled exec search" signal the scout was built for.

Everything in `new` becomes the digest:

1. The model scores the *unscored* ones (up to `MAX_SCORE_ITEMS = 40` per
   run, to bound the prompt for the small local model) — a 1–10 fit rating
   plus a one-line outreach angle, in a defensively parsed `id|score|angle`
   line format. Items past the cap, or lines the model skips/garbles, simply
   stay unscored; they still appear, sorted last in their section, with no
   score badge.
2. Python assembles the three-section HTML email (🆕 Just Funded / ⏳ Stalled
   Searches / 👋 Hiring Signals) — the model never formats the digest.
3. The email sends; anything scoring ≥ `OPP_SCORE_THRESHOLD` (default 8) also
   triggers an ntfy phone push.
4. Only after a successful send are the reported items marked `digested` and
   the EDGAR watermark advanced — a failed send loses nothing; the next run
   retries the same items.

Nothing new that day → no email.

## What does marking Interested or Dismissed do?

It changes only the item's status in the local store — it never affects what
gets polled.

- **Interested** — the item moves to the Interested section on
  `/opportunities`, auto-starts a company research brief (see below), and is
  kept indefinitely.
- **Dismissed** — the item leaves the triage view, will never be re-flagged
  as stalled, and is pruned from the store 30 days later. (`digested` items
  never touched also age out after 30 days.)

The store is the memory: a dismissed item that ages out could in principle
reappear if the source served it again, but in practice the EDGAR watermark
and HN's monthly thread turnover mean it won't.

## Will it grab the same entries on the next run?

No. EDGAR isn't re-asked about days already covered (the watermark). The ATS
boards and HN are re-fetched in full each run, but every returned item's id
already exists in the store, so nothing is re-inserted, re-scored, or
re-emailed. The one deliberate exception: a watched opening reappears in the
digest exactly once when it crosses the stalled threshold, because that's a
new signal about the same posting.

## Do scores change over time?

No — an item is scored once, when it's new, and the score is frozen. Known
gap: when an opening flips from `hiring` to `stalled_search` it re-enters the
digest but keeps its original score and angle, even though the stall is the
stronger signal. If that starts to matter (i.e. once the watchlist is
populated), the fix is to clear the score on flip so the next digest re-scores
it under its new signal.

## Research briefs

Marking an item Interested (or clicking its Research button) runs
`agent/tools/research.py`: a fixed pipeline — three bounded Tavily searches,
plus for EDGAR items a deterministic parse of the Form D filing XML (officer
names, offering/sold amounts, revenue range, industry) — summarized by the
model into a fixed-template brief (what they do / value prop / who to contact
/ size & stage / why now / recent news / red flags). The brief is stored on
the item and shown expandable on `/opportunities`, with an ntfy ping when
ready. `research_company` is the general-purpose sibling: research any company
by name from chat, nothing persisted.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `OPP_STATES` | `MA,NH,ME,VT,RI,CT` | EDGAR headquarters-state filter |
| `OPP_STALLED_DAYS` | `45` | Days open before a watched leadership posting flags as a stalled search |
| `OPP_SCORE_THRESHOLD` | `8` | Minimum score that triggers an ntfy push |
| `WREN_PUBLIC_URL` | unset | Base URL for the digest footer's link to `/opportunities`; footer omitted when unset |
