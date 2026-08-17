"""The learnings wiki as a link graph, for /wiki.

Wren could already answer "what do my notes say about X" (agent/tools/wiki.py)
and "is the vault healthy" (vault_health in chat/insights.py). Neither shows the
vault's *shape*: which pages are hubs, which sit alone, how a topic connects to
the rest. Obsidian draws that graph on the Mac mini; this makes it reachable from
anywhere Wren is, and lets a lint finding open the page it names in context.

Nodes are wiki pages, edges are [[links]] between them. Two exclusions do most of
the work of making the picture readable:

  index.md is not a node. It links every page in the vault, so drawn it is a
  388-degree hub that pulls the whole layout into a starburst and hides every
  real relationship. _list_wiki_pages already omits it (and log.md) for its own
  reasons, which happen to be the right ones here.

  A link to a page that doesn't exist is dropped, not turned into a node. An
  invented target is a lint finding (check_links), and materialising one would
  put a page on the map that isn't in the vault. They are counted instead, and
  the view shows the count as a pointer to /wiki/lint.

Read-only, Flask-free and standalone-runnable like chat/insights.py; the
blueprint that serves it is chat/routes_wiki.py.

Usage:
    python -m chat.wikigraph
"""

import re
import threading
from datetime import datetime

from agent.tools.wiki import (
    _HEAD_CHARS,
    _lens_meta,
    _list_wiki_pages,
    _project_meta,
    _require_vault,
    _SUMMARY_RE,
    _vault,
)

# Enough to identify a page in a hover card without doubling the payload. Page
# summaries run ~150-200 chars, so this trims most of them by a clause; the side
# panel shows the whole page anyway (/api/wiki/page/<name>).
MAX_SUMMARY_CHARS = 140

# Backticks and newlines are excluded from a link body, matching _LINK_RE in
# ObsidianWikiAgent's agent/wiki_tools.py. Not defensive tidiness — the naive
# `\[\[([^\]]+)\]\]` finds a real false link in this very vault:
# ai-chat-learnings-2026-08-02.md discusses wiki-link syntax, so a code span's
# `[[` opens a match that runs through the prose and closes on the ]] of the
# genuine [[obsidian-wiki-agent]] link after it. The invented target is dropped
# as dangling AND the real link is lost, which also makes its target look less
# connected than it is. Excluding backticks stops a code span from opening a
# match; excluding newlines stops one unclosed [[ eating the rest of the file.
_LINK_RE = re.compile(r"\[\[([^\]\n`]+)\]\]")
_TITLE_RE = re.compile(r"^# (.+?)\s*$", re.MULTILINE)
_UPDATED_RE = re.compile(r"^\*\*Last updated\*\*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)

# A slug ending in an ISO date is a dated chronological capture (daily-chrome-…,
# ai-chat-learnings-…), not a concept. Same rule as check_orphans in the sibling
# repo's wiki_lint.py, deliberately: the two views must agree on what a daily log
# is, or /wiki/lint reports an orphan that /wiki has filtered out of sight.
# ~100 of the 388 pages are these, and hiding them is the difference between a
# hairball and a map.
_DATED_LOG = re.compile(r"-\d{4}-\d{2}-\d{2}$")

# Same signature-cache shape as _TASKS_CACHE in chat/insights.py: the graph is a
# pure function of wiki/, so keying on that directory's (name, mtime_ns) makes an
# Obsidian edit invalidate it for free. Building it reads all 1.8 MB of the vault,
# which is fast but not free to redo on every poll. Locked; Flask runs threaded.
_GRAPH_CACHE: dict[str, tuple] = {}
_GRAPH_CACHE_LOCK = threading.Lock()


def link_targets(content: str) -> list[str]:
    """The page names a page links to, in order, with duplicates kept.

    Mirrors linked_page_names in ObsidianWikiAgent: an alias after '|' and an
    anchor after '#' are display detail, and a '.md' suffix is optional in
    Obsidian's own syntax. Reimplemented rather than imported — that repo is a
    subprocess boundary, not a dependency (see chat/wikilint.py).
    """
    names = []
    for raw in _LINK_RE.findall(content):
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if target.endswith(".md"):
            target = target[:-len(".md")]
        if target:
            names.append(target)
    return names


def _kind(slug: str, head: str) -> str:
    """What sort of page this is, for colour and for the view's filters.

    Order matters: a lens or a project page is always a hand-authored concept,
    while the dated test is a naming convention that nothing else can claim.
    """
    if _lens_meta(head) is not None:
        return "lens"
    if _project_meta(head) is not None:
        return "project"
    if _DATED_LOG.search(slug):
        return "log"
    return "concept"


def _node(slug: str, head: str) -> dict:
    summary = _SUMMARY_RE.search(head)
    title = _TITLE_RE.search(head)
    updated = _UPDATED_RE.search(head)
    text = summary.group(1) if summary else ""
    if len(text) > MAX_SUMMARY_CHARS:
        text = text[:MAX_SUMMARY_CHARS - 1].rstrip() + "…"
    return {
        "id": slug,
        "title": title.group(1).strip() if title else slug,
        "summary": text,
        "kind": _kind(slug, head),
        "updated": updated.group(1) if updated else "",
        "deg": 0,
    }


def _build_graph() -> dict:
    vault, err = _require_vault()
    if err:
        return err

    wiki_dir = vault / "wiki"
    nodes, heads, bodies = [], {}, {}
    for name in _list_wiki_pages(vault)["pages"]:
        slug = name[: -len(".md")]
        try:
            body = (wiki_dir / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        heads[slug] = body[:_HEAD_CHARS]
        bodies[slug] = body

    for slug in sorted(heads):
        nodes.append(_node(slug, heads[slug]))

    index = {n["id"]: i for i, n in enumerate(nodes)}

    # A set, so two links to the same page are one edge and A→B plus B→A is one
    # edge. The graph is undirected on screen; drawing the pair twice just makes
    # the line darker for no reason.
    seen, edges, dangling = set(), [], 0
    for slug, body in bodies.items():
        src = index[slug]
        for target in link_targets(body):
            if target not in index:
                dangling += 1
                continue
            dst = index[target]
            if dst == src:
                continue                      # a self-link is a lint finding
            pair = (min(src, dst), max(src, dst))
            if pair in seen:
                continue
            seen.add(pair)
            edges.append([pair[0], pair[1]])
            nodes[src]["deg"] += 1
            nodes[dst]["deg"] += 1

    return {
        "nodes": nodes,
        "edges": edges,
        "dangling": dangling,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _wiki_signature() -> tuple:
    wiki_dir = _vault() / "wiki"
    if not wiki_dir.is_dir():
        return ()
    sig = []
    for path in sorted(wiki_dir.glob("*.md")):
        try:
            sig.append((path.name, path.stat().st_mtime_ns))
        except OSError:
            continue
    return tuple(sig)


def build_graph() -> dict:
    """{nodes, edges, dangling, generated_at} — or {"error": ...} if the vault
    is missing. Cached on the vault's wiki/ signature (see _GRAPH_CACHE)."""
    signature = _wiki_signature()
    with _GRAPH_CACHE_LOCK:
        cached = _GRAPH_CACHE.get("entry")
        if cached is not None and cached[0] == signature:
            return cached[1]

    graph = _build_graph()
    if "error" not in graph:
        with _GRAPH_CACHE_LOCK:
            _GRAPH_CACHE["entry"] = (signature, graph)
    return graph


def main() -> int:
    graph = build_graph()
    if "error" in graph:
        print(f"error: {graph['error']}")
        return 1
    kinds: dict[str, int] = {}
    for node in graph["nodes"]:
        kinds[node["kind"]] = kinds.get(node["kind"], 0) + 1
    print(f"{len(graph['nodes'])} nodes, {len(graph['edges'])} edges, "
          f"{graph['dangling']} dangling link(s)")
    for kind, n in sorted(kinds.items()):
        print(f"{n:5d}  {kind}")
    hubs = sorted(graph["nodes"], key=lambda n: -n["deg"])[:5]
    print("hubs: " + ", ".join(f"{n['id']} ({n['deg']})" for n in hubs))
    orphans = [n["id"] for n in graph["nodes"] if n["deg"] == 0]
    print(f"orphans: {len(orphans)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
