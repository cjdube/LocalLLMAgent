# /wiki — the learnings wiki as a graph

Wren could already answer *"what do my notes say about X"* (`agent/tools/wiki.py`)
and *"is the vault healthy"* (`vault_health()` in `chat/insights.py`). Neither
shows the vault's **shape** — which pages are hubs, which sit alone, how a topic
connects to everything else. Obsidian draws that graph, but only on the Mac mini.
This makes it reachable from anywhere Wren is, and gives a lint finding somewhere
to point.

## What is on screen

Nodes are wiki pages; edges are `[[links]]` between them. Node size grows with
degree, colour with kind:

| Kind | Colour | What it is |
| --- | --- | --- |
| concept | cyan | an ordinary page |
| daily log | slate | a dated capture (`daily-chrome-2026-08-16`) — **off by default** |
| lens | amber | a page marked `lens: true`, what `evaluate_against` judges against |
| project | blue | a page marked `project: true` |

Today's vault: 388 pages, 926 links, 100 of them dated logs. Hiding the logs
leaves 288 pages and 475 links, which is the difference between a hairball and a
map — hence the default.

Selecting a page dims everything but it and its neighbours, and fills the panel
with its title, summary, date, every page it links to, and its full text on
demand. `orphans only` narrows to pages nothing links to, which is how you find
what a bad ingest stranded.

**`/wiki?page=<slug>` opens on a page.** That is what each finding on
[`/wiki/lint`](wiki-lint.md) links to. If a filter is hiding that page — a lint
finding about a dated log, most often — the filter is switched back on rather
than landing you on a graph that shows nothing.

## Two exclusions that make the picture readable

**`index.md` is not a node.** It links every page in the vault, so drawn it is a
388-degree hub that pulls the layout into a starburst and hides every real
relationship. `_list_wiki_pages` already omits it, and `log.md`, for its own
reasons — they happen to be the right ones here.

**A link to a page that doesn't exist is dropped, not drawn.** An invented target
is a lint finding (`check_links`); materialising one would put a page on the map
that isn't in the vault. They are counted, and the count shows in the header as a
link to `/wiki/lint`.

## Why the link parser is copied, not shared

`link_targets` in `chat/wikigraph.py` reimplements `linked_page_names` from
ObsidianWikiAgent, down to its regex. That repo is a subprocess boundary, not a
dependency — both projects have a top-level `agent` package, so importing across
is not available.

The regex excludes backticks and newlines from a link body, and that is not
tidiness. The naive `\[\[([^\]]+)\]\]` finds a false link in this very vault:
`ai-chat-learnings-2026-08-02.md` discusses wiki-link syntax, so a code span's
`[[` opens a match that runs through the prose and closes on the `]]` of the
genuine link after it. The invented target is dropped as dangling **and** the
real link is lost. The sibling repo hit this first and fixed it the same way; the
two must agree, or `/wiki` and `/wiki/lint` disagree about the same file.

## The layout

Canvas, not SVG: 388 nodes want pan and zoom on a phone, and 388 `<g>` elements
with transforms do not stay smooth there. No library — the project has no runtime
dependencies, and the simulation is about sixty lines.

- **Plain O(n²) repulsion, no quadtree.** 288 visible nodes is ~41k pairs per
  tick, well under a millisecond. A Barnes-Hut tree would be more code than the
  whole simulation and save nothing at this size.
- **Repulsion is weighted by degree.** A constant tuned to separate ordinary
  pages leaves this vault's hubs — `claude-code` and `agentos` carry ~50 edges
  each — welded into one blob with their labels stacked on top of each other.
- **Forces and step size are clamped.** Two nodes landing almost on top of each
  other produce an enormous inverse-square force; unclamped, one frame throws
  them to the far edge and the bounding-box fit then shrinks the whole vault to a
  speck. `MAX_STEP` is what allows several ticks per animation frame — the
  difference between a one-second settle and five.
- **It runs ~300 ticks and then freezes**, redrawing only on interaction. A
  permanent `requestAnimationFrame` loop on a page left open on a phone drains
  the battery for a picture that stopped moving. *Re-run layout* restarts it.
- **The camera is re-applied every frame while it settles**, not once at the end.
  Deferring it made the feature depend on the animation finishing, and it does
  not always finish: a browser stops delivering animation frames to a tab that
  isn't visible, so a `/wiki?page=…` link opened in a background tab settled
  part-way and then never centred.
- **Filters remove nodes from the simulation, not just the drawing**, so the
  remaining pages spread into the freed space.

## Cost

One payload, ~110 KB for the current vault, cached on the vault's `wiki/` mtimes
so an Obsidian edit invalidates it for free. Not paged: the view is a force
layout, and a partial graph doesn't lay out — nodes would jump every time more
arrived.

## Checking it without the browser

```bash
.venv/bin/python -m chat.wikigraph
```

Prints node and edge counts, a per-kind tally, the top hubs, and the orphan
count.
