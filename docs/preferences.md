# Personal preferences — `config/preferences.json`

Wren separates three kinds of configuration:

- **Secrets** (API keys, tokens) — `config/.env`, gitignored, documented in
  `config/.env.example`.
- **Runtime state** (memories, reminders, opportunity items) — gitignored JSON
  stores under `config/`, managed by the code.
- **Personal preferences** (who Wren serves and what they care about) —
  `config/preferences.json`, **committed to the repo**. Not secret, just
  personal. If you clone this repo, edit this one file to make Wren yours
  instead of editing Python.

The file is loaded once at import by `agent/prefs.py`. A missing or
unparseable file degrades to coded defaults (nothing crashes), but the
consumers below then run with generic/empty values, so keep it valid —
`tests/test_prefs.py` guards the schema. **Restart the chat server after
editing** (module-level values, including tool-schema enums, are built at
import).

## Keys

### `persona`

| Key | Used by | Purpose |
|---|---|---|
| `user_name` | scoring/research/colorizer/weekly-review prompts | The name the LLM prompts refer to |
| `positioning` | `tasks/opportunity_digest.py` scoring prompt, `agent/tools/research.py` brief prompt | One noun phrase: who you are professionally ("a fractional product/engineering leader (Vibe Foundry)") |
| `engagement_model` | `tasks/opportunity_digest.py` scoring prompt | One verb phrase completing "…who <engagement_model>." — how you engage with companies |

### `calendar`

`categories` — the list that drives everything calendar-colored. Each entry:

| Field | Required | Purpose |
|---|---|---|
| `name` | yes | Category label; becomes the `recolor_event` chat tool's enum and the colorizer's classification table |
| `color_id` | yes | Google Calendar colorId ("1"–"11") |
| `color_name` | yes | Google's name for that color (shown to the model in the classification table) |
| `hint` | no | Extra classification guidance appended in the colorizer prompt (e.g. "with others") |
| `role` | no | Operational tag — see below |

**Roles** decouple what the code needs (a "work bucket", a "fitness color")
from what you call your categories, so renaming "Work/LLC" to "Consulting"
breaks nothing. Recognized values, each expected on exactly one category:

- `work`, `meetings`, `appointments` — the weekly review's event buckets
  (`tasks/weekly_learnings.py`)
- `fitness` — the color Strava activities are logged with (`tasks/daily_log.py`)
- `fallback` — the colorId the colorizer uses when it can't classify an event
  (`tasks/calendar_colorizer.py`)

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
sites, so chat can answer "what was I doing on the AARP portal last week?" —
that's the difference between this list and `chrome_history.NOISE_DOMAINS`,
which blinds the tool everywhere.

An entry matches the domain **and its subdomains**, so `sharepoint.com` covers
every tenant (`aarpsharex.sharepoint.com`) and `aarp.org` covers
`secure.aarp.org`. It is not a substring match — `notsharepoint.com` is kept.
Ports are stripped before matching, so `127.0.0.1` would cover
`127.0.0.1:8420`. Empty list = nothing excluded.

Note that a service's own domain isn't always enough: the AARP volunteer portal
serves from both `aarpvolunteer.my.site.com` and `aarpvolunteer.my.salesforce.com`,
and both are listed. After adding an entry, check a real day against it (see
the exclusion tests in `tests/test_learnings_common.py`).

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
- **"Craig" in tool-schema description strings and module docstrings**
  (~40 mentions across `agent/tools/*.py`) and the weekly review's Obsidian
  template/section rules in `tasks/weekly_learnings.py` — a mechanical later
  sweep; the persona-bearing prompt openings already use `user_name`.
- **`agent/identity.md` / `agent/wren.md` / `agent/wren_chat.md`** — already
  data (committed Markdown loaded into system prompts); edit them directly.
