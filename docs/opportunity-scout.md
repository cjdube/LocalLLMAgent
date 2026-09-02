# The opportunity scout — how it works

The fractional-work opportunity scout finds leads for your practice from free,
ToS-clean sources, has the local model score them, and emails a weekly digest.
This page explains the full lifecycle: when it runs, what it looks at, how the
list is built, what triage does, and how deduping and scoring behave over
time.

Code: `tasks/opportunity_digest.py` (pollers, scoring, digest),
`agent/tools/opportunities.py` (the store and chat tools),
`agent/tools/research.py` (company research briefs),
`chat/views/opportunities.html` + routes in `chat/server.py` (the triage
page). It deliberately does NOT scrape LinkedIn or use paid data SaaS — see
`AGENTS.md`'s data sourcing policy.

## When does it run?

Weekly, Sundays at 9:00 PM, via launchd
(`launchd/local.wren.opportunitydigest.plist`). You can also
ask Wren in chat to "send the opportunity digest" anytime — the chat tool
(`send_opportunity_digest`) runs the exact same pipeline
(`build_and_send_digest()`), behind a tap-to-confirm since it sends an email.

## What is it looking at?

Three sources, each polled fresh every run. Each degrades to empty on error —
one dead feed never kills the digest. A degraded poll is logged at WARNING, so
`log_inspector` reports it the next morning; the run itself still counts as a
success, because the digest did its job with the sources that answered. EDGAR
retries a transient failure (5xx, timeout) up to `_EDGAR_RETRIES` times before
giving up — its full-text search 500s occasionally, and one attempt per week
turned a blip into a skipped week. A 4xx is not retried: that's a malformed
request on our side, and repeating it only wastes the window.

1. **SEC EDGAR Form D filings** ("just funded") from companies headquartered
   in the watched states (`OPP_STATES`, default `MA,NH,ME`). Only filings
   since the last successful run are requested, tracked by a watermark date in
   `config/opportunities_state.json`. Fund/trust/SPV/series-LLC paperwork is
   filtered out by name (`_FUND_NAME_RE`), and multiple same-day filings by
   one filer collapse into a single entry with an "(N filings)" count.

   Each state is paged until EDGAR's own reported hit total is exhausted, with
   `_EDGAR_MAX_PAGES` (300 filings/state) as a safety cap so a long catch-up
   can't fire unbounded requests. EDGAR returns newest-first, so hitting that
   cap would mean the *oldest* filings in the window went unseen — rather than
   advancing the watermark past them, the poll reports the oldest date it
   actually reached and the watermark holds there, so the remainder is picked
   up (and deduped) next run. Hitting the cap logs a warning.
2. **The ATS watchlist** — the public job boards of companies added to the
   watchlist, filtered to product/eng leadership titles (VP/Head/Director/
   CPO/CTO of product or engineering). Four ATSes are supported; the board
   slug is the company's identifier in its careers-page URL:
   - Greenhouse (`boards.greenhouse.io/<slug>`), Lever
     (`jobs.lever.co/<slug>`), Ashby (`jobs.ashbyhq.com/<slug>`) — public
     JSON board APIs, with real posted dates.
   - iCIMS (`<slug>.icims.com`, common at larger companies) — no public JSON
     API, so the scout reads the portal's robots.txt-published sitemap
     instead: job id and title come from the URL, and since the sitemap has
     no trustworthy posted date, the stalled clock runs from when Wren first
     sees the opening.

   Curate the watchlist in chat ("watch Acme on greenhouse as acme") or on
   the `/opportunities` page.
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

Then the closure check runs: for every watched board that answered **without
an error this run**, any stored opening whose id isn't in that board's current
openings has come down — filled, pulled, or retitled past the leadership
filter. The board-by-board scoping is the whole safety of it: a timed-out
board returns zero openings, and without it that would retire a company's
entire pipeline in one go. A company the user just unwatched is likewise never
polled, so its items are left alone.

Everything in `new` becomes the digest:

1. The model scores the *unscored* ones — a 1–10 fit rating plus a one-line
   outreach angle, in a defensively parsed `id|score|angle` line format.
   Scoring runs in batches of `MAX_SCORE_ITEMS = 40` (one model call each) to
   bound each prompt for the small local model; every item gets scored, however
   many batches that takes. Lines the model skips or garbles simply stay
   unscored; those items still appear, sorted last in their section, with no
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

## What happens when a posting is filled?

The closure check above retires it, and the status it lands in depends on where
it was:

- Not yet triaged (`new` or `digested`) → status `closed`. It leaves the triage
  view immediately and ages out on the same 30-day clock as `dismissed`. A
  posting that closes before its first digest is dropped from that digest
  rather than emailed as a live lead — the check runs before the digest is
  built.
- `interested` → **kept, with a "no longer listed" badge** on `/opportunities`.
  the user may already have emailed them, so the closure is information, not a
  reason to hide the item. It keeps its research brief and never ages out.
- `dismissed` → untouched.

`closed` is deliberately separate from `dismissed`: dismissed means the user
judged it, closed means the market did. Conflating them would lose the record
of which watched searches actually resolved. Nothing pushes a notification for
a closure; the badge and the log line are the whole surface.

This is ATS-only. An EDGAR Form D filing is a historical event that never
closes (a stale raise decays in relevance rather than disappearing), and HN
comments are never removed — only board postings have a knowable end.

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

An item is scored when it's new and the score then stays put — with one
deliberate exception: when an opening flips from `hiring` to `stalled_search`,
the flip sheds the old score and outreach angle, so the next digest re-scores
it under the stronger signal (the scoring prompt anchors long-open leadership
seats high). Nothing else re-scores; interested/dismissed/digested items keep
whatever score they got.

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
| `OPP_STATES` | `MA,NH,ME` | EDGAR headquarters-state filter |
| `OPP_STALLED_DAYS` | `45` | Days open before a watched leadership posting flags as a stalled search |
| `OPP_SCORE_THRESHOLD` | `8` | Minimum score that triggers an ntfy push |
| `WREN_PUBLIC_URL` | unset | Base URL for the digest footer's link to `/opportunities`; footer omitted when unset |
