# Personal preferences — `config/preferences.json`

Wren separates three kinds of configuration:

- **Secrets** (API keys, tokens) — `config/.env`, gitignored, documented in
  `config/.env.example`.
- **Runtime state** (memories, reminders, opportunity items) — gitignored JSON
  stores under `config/`, managed by the code.
- **Personal preferences** (who Wren serves and what they care about) —
  `config/preferences.json`, **gitignored**. Not secret, just personal — it
  holds your name, where you live, and what you do, none of which belongs in a
  shared repo. `config/preferences.example.json` is the committed template:
  copy it, edit your copy, and never edit Python.

The file is loaded once at import by `agent/prefs.py`, which falls back to
`preferences.example.json` when you haven't made your own copy yet — so a fresh
clone boots with a valid schema. A file that exists but is unparseable degrades
to coded defaults (nothing crashes), but the consumers below then run with
generic/empty values, so keep it valid — `tests/test_prefs.py` guards the
schema of whichever file is live. **Restart the chat server after editing**
(module-level values, including tool-schema enums, are built at import).

## Keys

### `persona`

| Key | Used by | Purpose |
|---|---|---|
| `user_name` | scoring/research/colorizer/daily-learnings prompts | The name the LLM prompts refer to |
| `positioning` | `tasks/opportunity_digest.py` scoring prompt, `agent/tools/research.py` brief prompt | One noun phrase: who you are professionally ("a fractional product/engineering leader") |
| `engagement_model` | `tasks/opportunity_digest.py` scoring prompt | One verb phrase completing "…who <engagement_model>." — how you engage with companies |

### `calendar`

`categories` — the list that drives everything calendar-colored. Each entry:

| Field | Required | Purpose |
|---|---|---|
| `name` | yes | Category label; becomes the `recolor_event` chat tool's enum and the colorizer's classification table |
| `color_id` | yes | Google Calendar colorId ("1"–"11"); need not be unique |
| `color_name` | yes | Google's name for that color (shown to the model in the classification table) |
| `hint` | no | Extra classification guidance appended in the colorizer prompt (e.g. "with others") |
| `role` | no | Operational tag — see below |

Several categories may share one `color_id` — Travel, Dining Out, and
Shows/Events are all Peacock. Distinct names classify better than one
grab-bag category with a long `hint`: the model matches an event title
against a label, and each name is separately selectable in `recolor_event`.

**Roles** decouple what the code needs (a "work bucket", a "fitness color")
from what you call your categories, so renaming "Work/LLC" to "Consulting"
breaks nothing. Recognized values, each expected on exactly one category:

- `fitness` — the color Strava activities are logged with (`tasks/strava_download.py`)
- `fallback` — the colorId the colorizer uses when it can't classify an event
  (`tasks/calendar_colorizer.py`)
- `work`, `meetings`, `appointments` — **legacy, no consumer.** These were the
  weekly review's event buckets; the weekly review was split into the daily
  learnings tasks, which then dropped calendar bucketing entirely. Kept on their
  categories (and asserted by `tests/test_prefs.py`) so the tags are already in
  place if event bucketing returns. Nothing reads them today — retagging or
  removing them changes no behavior.

### `learnings`

What the daily learnings reviews ignore. Scoped to those tasks only:
`fetch_chrome_history` still returns this data, so chat can answer questions
about it.

`excluded_keywords` — subject matter kept out of the review entirely, whatever
it's hosted on. Case-insensitive substrings, matched against:

- a **browsed site's title** — the site is dropped
- a **single page path** — that path is dropped, the site is kept

Substring matching is blunt on purpose (the goal is keeping a subject out of the
vault, not classifying it), so pick distinctive terms. A short or common term
will over-match.

`excluded_domains` — domains kept out of the daily learnings reviews
(`tasks/_learnings_common.py:compact_sites`). Volunteer-admin portals and
Microsoft 365 live here: real activity, but not something to review.

Scoped to the learnings tasks only. `fetch_chrome_history` still returns these
sites, so chat can answer "what was I doing on the volunteer portal last week?"
— that's the difference between this list and `chrome_history.NOISE_DOMAINS`,
which blinds the tool everywhere.

An entry matches the domain **and its subdomains**, so `sharepoint.com` covers
every tenant (`acme.sharepoint.com`) and `example.org` covers
`secure.example.org`. It is not a substring match — `notsharepoint.com` is kept.
Ports are stripped before matching, so `127.0.0.1` would cover
`127.0.0.1:8420`. Empty list = nothing excluded.

Note that a service's own domain isn't always enough: a Salesforce-backed
volunteer portal can serve from both `<org>.my.site.com` and
`<org>.my.salesforce.com`, and both need listing. After adding an entry, check a
real day against it (see the exclusion tests in
`tests/test_learnings_common.py`).

### `job_search`

Drives the opportunity scout (`tasks/opportunity_digest.py`,
[opportunity-scout.md](opportunity-scout.md)).

| Key | Purpose |
|---|---|
| `seniority_terms` | Whole words: vp, head, director… |
| `function_terms` | **Prefix-matched** (no trailing word boundary): "technolog" covers technology/technologies |
| `title_acronyms` | Standalone whole-word titles: cto, cpo… |
| `hn_phrases` | Phrases that make an HN "Who is hiring" post worth scoring (whole-word matched) |
| `states` | US states whose new SEC Form D filings count as a "funded" signal (env `OPP_STATES` overrides) |

A watched ATS board's job title is flagged when a seniority term appears
within 40 characters of a function term, or a title acronym appears on its
own. All terms are matched case-insensitively and regex-escaped — plain words
and phrases only, no regex syntax.

### `location`

`City,ST,US` for weather and the morning brief. Env `DEFAULT_LOCATION`
overrides.

## Deliberately NOT externalized (for now)

- **Memory category tags** (`agent/tools/memory.py` `CATEGORIES`) — generic
  vocabulary (preference, person, schedule…), no personal data.
- **The EDGAR fund-name noise filter** (`_FUND_NAME_RE` in
  `tasks/opportunity_digest.py`) — SEC Form D mechanics, not a preference;
  anyone scouting the same source wants the same filter.
- **The daily reviews' Obsidian template/section rules** in
  `tasks/daily_*_learnings.py` — structural output contract, not a preference.
- **`agent/wren.md` / `agent/wren_chat.md`** — Wren's own voice and her
  interactive behavior, deliberately impersonal; edit them directly if you want
  a different agent. `agent/identity.md` (who she serves) is the personal one:
  gitignored like `preferences.json`, templated by
  `agent/identity.example.md`.
