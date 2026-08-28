# The project registry — how it works

Wren scans the user's local checkouts each morning and turns each one into an
anchor for [daily synthesis](daily-synthesis.md), so the prior day's reading can
be matched against **what he is actually building** — not just what he has
written notes about.

## Why it exists

The wiki already had project pages, and they were second-hand. A project earned
a page only if it happened to come up in a day's browsing or chat log, so the
coverage was uneven (6 of 12 checkouts had a page, and `AgenticOS`,
`AIChatScraper`, `WorkoutTimer`, `ai-memory`, `my-agent-hq` and `app-evaluator`
had none), and the pages that existed described the project through whatever the
log said rather than through the repo. The page for the `LocalLLMAgent` checkout
summarised it as *"an internal agentic toolset … integrated with Google Cloud
Console OAuth authentication, process control safeguards, and interactive
debugging"* — assembled from browsing history, with sources including a page
about birds. Nothing re-read the repo, so a page froze at whatever the last log
said.

More importantly, `daily_synthesis` had no way to reach the projects at all. Its
anchors were wiki pages and watched companies, so it could connect the day's
reading to things he had *written down* but never to the code he was in the
middle of. An article on SSE reconnection has nothing to say to a page about
note-taking and plenty to say to the repo that just moved to server-sent events.

## The pieces

| Piece | Role |
|---|---|
| `agent/tools/projects.py` | Deterministic scan of `PROJECTS_DIR`, the `config/projects.json` store, and the two chat tools. |
| `tasks/project_scan.py` | Distils each project to a summary + topics; writes the store. Daily 5:30 AM. |
| `agent/tools/wiki.py` (`list_project_pages`) | Finds the vault pages marked `project: true`. |
| `tasks/daily_synthesis.py` (`gather_project_anchors`) | Merges the two into one anchor per project. |

### The scan

`scan_projects()` walks each direct subdirectory of `PROJECTS_DIR` (default
`~/Projects`) and reads, per project:

- git freshness — remote, branch, last commit day (`%cs`, never a sliced ISO
  stamp), commits in the last 30 days, and whether the tree is dirty
- its README, the first root instruction file named by
  `projects.instruction_files` (`AGENTS.md` by default; compatibility names may
  be added in preferences), and the first heading of each `docs/*.md`

**Only those three documents are ever read.** Not `.env`, not `config/*.json`,
not anything else that happens to sit in a project directory — the registry
travels into prompts, and project directories routinely hold secrets. The read
list in `agent/tools/projects.py` is exhaustive on purpose and is pinned by a
test.

A directory that isn't a git repo is an ordinary outcome (5 of 12 aren't), not
an error: the git fields come back `null` and the documents are still read. A
checkout that fails outright degrades to a row carrying its error, so one broken
directory never costs the others.

### The distillation

`tasks/project_scan.py` makes **one isolated model call per project** — the
project count drives the number of calls, not the size of any prompt — asking
for a fixed two-line template:

```
summary: <one plain sentence saying what this project is and does>
topics: <8 to 15 comma-separated technical terms this project is about>
```

`think=False`, because this fills in a template (see the AGENTS.md rule: on a
reasoning-heavy call the thinking tokens come out of the same `num_predict`
budget and the call returns *empty content*).

Blurbs are cached in `config/projects.json` and keyed on a `content_hash` of the
three documents, so a project is re-distilled only when its documentation
actually changes. A commit that touches no docs doesn't invalidate the cache, so
the daily run is normally a git refresh and zero model calls. `--refresh`
regenerates everything.

Three degradations are reported rather than swallowed, because all three are
otherwise invisible — the project still appears in the registry and simply stops
matching anything:

- a project with no README, configured instruction file, or `docs/` is named in a WARNING
  (on the real machine: `AgenticOS`, `AIChatScraper`, `my-agent-hq`,
  `SortOfCardGame` — the fix is a README in that repo, not code here). The one
  exception where code *was* the fix: `AgenticDevelopment` had `AGENTS.md` and
  nothing else, so the read list grew that spelling rather than the repo growing
  a duplicate README
- a project whose `docs/` tree outgrew `MAX_DOC_TITLES` (40) is named with the
  pre-cap count, since `_doc_titles` drops the alphabetical tail. The blurb still
  reads normally; it just stops reflecting part of what the project documents.
  LocalLLMAgent sat at exactly 20 when this was added, and tripped the cap again
  at 21 and at 32 — each time the WARNING is what said so
- a distillation returning fewer than 4 topics is named with counts

The cap itself stays — a few dozen one-line titles is the right bound for a
prompt, and a project with a 60-page docs tree shouldn't spend it all here. What
was wrong was truncating in silence. What the cap does and does not protect (it
guards the signal ratio inside one prompt, not the context window, and never
touches the anchor) is written up in
[docs/limits.md](limits.md#what-max_doc_titles-actually-guards).

### Why a distillation and not the raw docs

A project has far more text behind it than a wiki page — a README, an instruction file,
a docs tree. Feeding that in raw would rebuild a bug this repo has already hit
once: see the comment on `daily_synthesis._ai_chat_signals`, which reads the
*distilled* AI-chat log rather than raw transcripts because "a token set that
large overlaps everything, so it would match every anchor and rank above every
real pair."

So the model's output is deliberately small, and `_project_tokens` enforces a
hard ceiling (`MAX_PROJECT_ANCHOR_TOKENS`, 40) on top of it. Tokens are filled
in priority order — name, then summary, then topics — so truncation costs the
tail of the topic list, never the project's own name. A project anchor therefore
stays in the same size class as a wiki page's (~20–25 tokens).

The summary is separately bounded by `_summary_head`, to the same
`MAX_ANCHOR_SUMMARY_CHARS` the *displayed* summary gets. This was not
theoretical: the merge prefers the wiki page's summary, and `wren.md`'s ran to
30 words — "…modeled after the high-output, agile characteristics of the wren
bird" — which was more than the project's name and all ten of its topics
combined. It pushed `rest`, `tailscale` and `tool` off the end, so a page
describing the project *badly* was displacing terms taken from the repo itself.
Preferring the wiki summary is only right when the page is good; the bound is
what makes it safe when it isn't. With it, the busiest project sits at 34 and
the ceiling is a backstop rather than the thing doing the work.

## The wiki join

A project has two halves, and they live in different places:

- **What it is, right now** — the scan. Always current, because it re-reads the
  repo.
- **Why it is that way** — the wiki page. Decisions, trade-offs, what was tried
  and abandoned. The repo's own README does not contain this, and the scan can
  never produce it.

A wiki page opts in as a project page with frontmatter, exactly the way a
[lens](lenses.md) opts in with `lens: true`:

```markdown
---
project: true
repo: cjdube/WeighAnchor
path: WeighAnchor
---
```

`path` is the join key, and it has to be explicit: **page names and directory
names routinely disagree.** The page for the `LocalLLMAgent` checkout is
`wren.md`. Nothing about those two strings matches, and `sort-of-card-game` has
to be told apart from `umbrella-card-game`. No slug rule can do this.

`gather_project_anchors` emits **one** anchor per project, preferring the wiki
page's summary (it carries the reasoning) and returning the absorbed page name so
the wiki loop skips it. Without the merge, a project stands as two separate
anchors; since `_one_per_side` dedupes by side *identity*, both would place and
the model would be shown the same story twice — the exact failure that function
exists to prevent.

A project page whose checkout is gone stays an ordinary wiki anchor. A project
with neither a summary nor topics is skipped entirely: anchoring on a bare name
can only match its own spelling, which is the tautology `gather_anchors` warns
about.

## What it looks like on real data

From the first live run (12 checkouts, 8 documented):

```
9 project anchor(s), 6 merged with a wiki page
26 signal(s) {'chrome': 22, 'youtube': 1, 'ai-chat': 3}, 230 anchor(s)
5 candidate(s) after overlap match
```

Nine anchors rather than eight because `SortOfCardGame` has no README but *does*
have a wiki page — the merge picks it up from the vault side alone.

Projects took **1 of the 5** candidate slots, competing against ~220 wiki pages
on match strength alone. That answers the question the design deliberately left
open: they neither flood the shortlist nor get shut out, so no reserved slot or
per-kind cap is needed. Revisit it from a dry run, not from a guess, if that
stops being true.

## The chat tools

Two read-only tools in the deferred `projects` group (see
[docs/tool-loading.md](tool-loading.md)):

- **`list_projects`** — every checkout with its summary, last commit day,
  30-day commit count and dirty flag.
- **`read_project(name)`** — one project in full, plus its topics, docs page
  titles, and its wiki page if it has one. Name matching is deliberately
  forgiving about case and spacing, because the model is passing back a name
  the user typed, not an identifier it was handed: `weigh anchor` finds
  `WeighAnchor`.

Both scan the checkouts **live** and merge the cached distillation, rather than
serving the registry alone. The summary and topics need a model call so they
come from the nightly cache, but "when did I last commit" and "is the tree
dirty" are what a chat ask is usually really about, and a cached answer to
those is wrong for up to 24 hours. A full scan is ~300ms, which a chat turn can
afford. Same live-fetch/cached-blurb split the [starred view](starred.md) uses.

### The description does most of the work

This is a catalogue tool — the one shape where pretraining supplies a
*plausible* answer, so the model skips the tool and invents entries. `list_games`
hit exactly this: asked the vague "let's play a game" with a description that
only said *when* to call it, the model named Wordle, Sudoku and Chess with
fabricated links in 2 of 12 replays. The risk is worse here, because an invented
project name reads as completely ordinary — nothing in the reply would look
wrong, and no tool ran, so nothing is logged.

So the description states flatly that the list is **not something the model
knows**, that only what the tool returns exists, and what to say when it returns
nothing. The same denial is repeated in the group blurb, because when the group
hasn't been pre-loaded the model can only see the blurb, not the description.
Both are pinned by tests.

Measured over 24 replays against the live model, 8 phrasings:

| Phrasing | Called the tool |
|---|---|
| "what am I working on?" | 3/3 |
| "tell me about my projects" | 3/3 |
| "what have I built?" | 3/3 |
| "which of my repos have I not touched in a while?" | 3/3 |
| "what code have I written lately?" (no keyword → `load_tools` hop) | 3/3 |
| "show me everything I have made" (`load_tools` hop) | 3/3 |
| "what is in my dev folder?" (`load_tools` hop) | 3/3 |
| "give me a rundown of my stuff" | 0/3 |

**Zero fabricated projects in all 24 runs** — the failure this guards against
never occurred. The last row is not that failure: on a genuinely ambiguous ask
the model read "my stuff" as the day ahead and answered correctly from calendar,
tasks and reminders. Widening the keywords to catch it would misfire constantly,
so it's left alone.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PROJECTS_DIR` | `~/Projects` | Where the checkouts live |

`config/projects.json` is gitignored (local runtime state) and redirected to
`tmp_path` suite-wide by `tests/conftest.py`, along with `PROJECTS_DIR` itself —
the scanner reads the machine, so an unpinned test would walk the developer's
real checkouts and shell out to git for each one.

## Running it by hand

```bash
.venv/bin/python -m agent.tools.projects --brief
```

```bash
.venv/bin/python -m tasks.project_scan --refresh
```
