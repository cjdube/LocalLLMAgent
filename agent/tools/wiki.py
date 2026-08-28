"""Read-only access to the user's personal learnings wiki (an Obsidian vault) so
Wren can answer "what did I decide about X" from their own notes.

The vault at WIKI_VAULT_PATH is built and maintained by the sibling
ObsidianWikiAgent project. These tools let the agent loop navigate it: search
for the topic, open the page(s) that come back, answer.

Searching is the entry point because listing isn't one. The two listing tools
that used to be the entry point — read_wiki_index (the whole of wiki/index.md)
and list_wiki_pages (390 filenames) — are 62KB and 9.7KB against the 8000-char
OLLAMA_MAX_TOOL_RESULT_CHARS cap in agent/loop.py, so both were cut to a prefix
the model had no way to know was a prefix. Five of the index's six sections were
never visible at all, and Wren answered "no such page" about pages that exist.
Both are unregistered now; search_wiki returns rows, so its size scales with the
answer instead of the vault. ObsidianWikiAgent hit the same wall on its write
side and replaced read_index with list_index_sections for the same reason.

Vault layout (see ObsidianWikiAgent):
    <vault>/wiki/index.md   -- table of contents the model reads first
    <vault>/wiki/*.md       -- concept pages (excluding index.md, log.md)

wiki/ is the only part of the vault Wren reads. The sibling <vault>/raw/ is a
write-only handoff: the daily learnings tasks drop files there via
learnings_file.write_entry, and ObsidianWikiAgent owns everything downstream —
it files them into subdirectories and summarizes them into wiki/ pages. Don't
add tools that read raw/. Two of them existed and were removed: the reorganized
layout made them silently return nothing, and they offered no capability wiki/
doesn't, since processed reviews land there as ordinary concept pages
(strategic-weekly-review-<date>.md).

A missing vault dir is surfaced as an error (like learnings_file.py) rather than
raising, so a misconfigured WIKI_VAULT_PATH degrades to "no wiki" instead of
breaking the chat turn that asked. Functions read from a single
configured vault; the internal helpers take vault_path explicitly so pointing at
another vault later is a config change, not a rewrite.

Usage:
    python -m agent.tools.wiki search "product leadership"
    python -m agent.tools.wiki read-page speakers-bureau
    python -m agent.tools.wiki read-index    # human-only; too big for the model
    python -m agent.tools.wiki list-pages    # human-only; same reason
"""

import argparse
import os
import re
import sys
from pathlib import Path

from agent import prefs
from agent.tools._http import load_env, print_result

# Whose wiki this is, for the model-facing strings below. From
# config/preferences.json; falls back to "the user".
_NAME = prefs.user_name()

load_env()

DEFAULT_WIKI_VAULT = str(Path.home() / "Vaults" / "llm-wiki-learnings")

# A "lens" is an ordinary wiki page that opts in as one of the user's standards
# rubrics by declaring `lens: true` in its YAML frontmatter — the thing
# evaluate_against judges a target against. Nothing else distinguishes it from a
# concept page, so the marker is how Wren tells lenses from the 180-odd other
# notes. Keep the injected index cheap (same budget reasoning as the skills
# index): the chat prompt already crowds num_ctx.
MAX_INDEX_LENSES = 8
MAX_INDEX_CHARS = 600

# search_wiki's own caps. Both exist so a broad query ("project") can't hand back
# most of a 390-page vault and get trimmed by the 8000-char tool-result cap — the
# exact failure that made read_wiki_index useless. Summaries run ~150-200 chars,
# so the row cap usually binds first; the char budget covers the long tail. A
# summary can't exceed _HEAD_CHARS, so the last row can overshoot the budget by
# at most that much and the worst case still lands under 8000.
MAX_SEARCH_RESULTS = 20
MAX_SEARCH_CHARS = 4000

# read_wiki_page's own budget, in page chars — under the 16000-char backstop
# agent/loop.py gives this tool (TOOL_RESULT_CHAR_CAPS), because that backstop
# counts the JSON-escaped result and markdown's newlines roughly double on the
# way through. The gap is the escaping headroom; widen both together or the
# blind backstop cuts the [[link]] footer _fit_page went out of its way to keep.
# 14000 covers every page in the vault but one (agentos, at ~18KB).
MAX_PAGE_CHARS = 14000

# Room _fit_page sets aside for its "here's what I cut" notice. The boilerplate
# is ~330 chars; the rest covers the dropped section names (10 on the largest
# page in the vault).
_NOTICE_RESERVE = 700

# Frontmatter lives at the very top; read only a bounded head to classify a page
# without pulling whole bodies for all of them every turn.
_HEAD_CHARS = 2048
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)

# Every pattern below matches horizontal whitespace only ([ \t]), never `\s`.
# `\s` matches newlines, so around an EMPTY value it eats the line break and the
# capture group takes the next frontmatter line instead. `repo:` on the real
# vault came back as "path: screenwatch-kit" that way, and a lens whose author
# leaves `description:` blank would have had the following key injected into the
# chat system prompt as its description. An empty value is a normal thing to
# write, so the captures are `(.*?)` — an absent key and a blank one both mean
# "no value", and the callers already treat them the same.
_LENS_RE = re.compile(r"^[ \t]*lens:[ \t]*true[ \t]*$", re.IGNORECASE | re.MULTILINE)
_DESC_RE = re.compile(r"^[ \t]*description:[ \t]*(.*?)[ \t]*$", re.IGNORECASE | re.MULTILINE)

# A page about one of the user's own projects opts in the same way a lens does,
# with `project: true` plus the checkout it describes. The marker exists because
# the page name and the directory name routinely disagree and no amount of slug
# matching can bridge it: the page for the LocalLLMAgent checkout is `wren.md`,
# and `sort-of-card-game` has to be told apart from `umbrella-card-game`. `path`
# is what tasks/daily_synthesis.py joins on; `repo` is recorded for readers.
_PROJECT_RE = re.compile(r"^[ \t]*project:[ \t]*true[ \t]*$", re.IGNORECASE | re.MULTILINE)
_REPO_RE = re.compile(r"^[ \t]*repo:[ \t]*(.*?)[ \t]*$", re.IGNORECASE | re.MULTILINE)
_PATH_RE = re.compile(r"^[ \t]*path:[ \t]*(.*?)[ \t]*$", re.IGNORECASE | re.MULTILINE)

# ObsidianWikiAgent gives every concept page a one-line `**Summary**:` under its
# title — all 203 pages had one when this was added. It's the cheapest description
# of what a page is *about*, as opposed to what it's called.
_SUMMARY_RE = re.compile(r"^\*\*Summary\*\*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)

# Query terms for search_wiki. Splitting on non-alphanumerics folds punctuation
# and the slug hyphens in page names into the same shape.
_TERM_RE = re.compile(r"[a-z0-9]+")

# The two parts of a page _fit_page treats as special, and its section names.
# `**Sources**:` is ObsidianWikiAgent's ingest provenance — 16 filenames and
# ~500 chars on the big pages, of no use to a reader. `## Related pages` is the
# [[link]] graph and is always the LAST section, so a blind tail cut takes the
# page's navigation first, every time.
_SOURCES_LINE_RE = re.compile(r"^\*\*Sources\*\*:.*$\n?", re.MULTILINE)
_RELATED_RE = re.compile(r"^## Related pages\b.*", re.MULTILINE | re.DOTALL)
_H2_RE = re.compile(r"^## (.+?)\s*$", re.MULTILINE)


def _vault() -> Path:
    return Path(os.getenv("WIKI_VAULT_PATH", DEFAULT_WIKI_VAULT)).expanduser()


def _require_vault() -> tuple[Path | None, dict | None]:
    """Return (vault, None) or (None, error) if the vault dir is missing — a
    wrong WIKI_VAULT_PATH, or the vault moved. Mirrors learnings_file.py so Wren
    degrades gracefully instead of raising."""
    vault = _vault()
    if not vault.exists():
        return None, {"error": f"learnings vault not found (check WIKI_VAULT_PATH): {vault}"}
    return vault, None


def _safe_child(base: Path, name: str) -> Path:
    """Resolve `name` to a path inside `base`, rejecting any attempt to escape
    it (e.g. via '../'). The name comes from the model, so it's untrusted."""
    candidate = (base / name).resolve()
    base = base.resolve()
    if base not in candidate.parents and candidate != base:
        raise ValueError(f"'{name}' resolves outside {base}")
    return candidate


# --- internal helpers, vault-parameterized (lifted from ObsidianWikiAgent) ---

def _read_index(vault: Path) -> dict:
    path = vault / "wiki" / "index.md"
    if not path.is_file():
        return {"content": ""}
    return {"content": path.read_text(encoding="utf-8")}


def _list_wiki_pages(vault: Path) -> dict:
    wiki_dir = vault / "wiki"
    if not wiki_dir.exists():
        return {"pages": []}
    pages = sorted(
        p.name for p in wiki_dir.iterdir()
        if p.is_file() and p.suffix == ".md"
        and not p.name.startswith(".") and p.name not in ("index.md", "log.md")
    )
    return {"pages": pages}


def _read_wiki_page(vault: Path, name: str, section: str | None = None) -> dict:
    filename = name if name.endswith(".md") else f"{name}.md"
    try:
        path = _safe_child(vault / "wiki", filename)
    except ValueError as e:
        return {"error": str(e)}
    if not path.is_file():
        return {"error": f"wiki page '{name}' not found"}

    text = path.read_text(encoding="utf-8")
    if section is None or not section.strip():
        return {"content": _fit_page(text)}

    sections = _split_sections(_SOURCES_LINE_RE.sub("", text, count=1))
    if not sections:
        return {"error": f"wiki page '{name}' has no sections — read it without a section argument"}
    hit = _match_section(sections, section)
    if hit is None:
        # Name what exists rather than just saying no, so the retry is informed.
        return {"error": f"no section like '{section}' in wiki page '{name}'",
                "sections": [heading for heading, _ in sections]}
    heading, body = hit
    return {"page": name, "section": heading, "content": _fit_page(body)}


def _lens_meta(head: str) -> dict | None:
    """If `head` (a page's leading bytes) declares `lens: true` in its
    frontmatter, return {"description": ...}; else None. The description is the
    frontmatter line, used for the injected index — not the page body."""
    m = _FRONTMATTER_RE.search(head)
    if not m or not _LENS_RE.search(m.group(1)):
        return None
    desc = _DESC_RE.search(m.group(1))
    return {"description": desc.group(1).strip() if desc else ""}


def _list_lenses(vault: Path) -> list:
    """Every wiki page marked `lens: true`, as {name, description} rows. Reads
    only each page's head to classify it, so scanning the whole vault stays
    cheap enough to run per chat turn."""
    lenses = []
    for name in _list_wiki_pages(vault)["pages"]:
        try:
            head = (vault / "wiki" / name).read_text(encoding="utf-8")[:_HEAD_CHARS]
        except OSError:
            continue
        meta = _lens_meta(head)
        if meta is not None:
            lenses.append({"name": name[:-3], "description": meta["description"]})
    return lenses


def _project_meta(head: str) -> dict | None:
    """If `head` (a page's leading bytes) declares `project: true` in its
    frontmatter, return {"repo": ..., "path": ...}; else None. Mirrors
    _lens_meta. A page may declare the marker without a `path` — it's still a
    project page, it just can't be joined to a checkout, which the caller
    reports rather than guessing at."""
    m = _FRONTMATTER_RE.search(head)
    if not m or not _PROJECT_RE.search(m.group(1)):
        return None
    repo = _REPO_RE.search(m.group(1))
    path = _PATH_RE.search(m.group(1))
    return {"repo": repo.group(1).strip() if repo else "",
            "path": path.group(1).strip() if path else ""}


def _list_project_pages(vault: Path) -> list:
    """Every wiki page marked `project: true`, as {name, repo, path, summary}
    rows. Head-reads each page to classify it, like _list_lenses, so scanning
    the whole vault stays cheap."""
    projects = []
    for name in _list_wiki_pages(vault)["pages"]:
        try:
            head = (vault / "wiki" / name).read_text(encoding="utf-8")[:_HEAD_CHARS]
        except OSError:
            continue
        meta = _project_meta(head)
        if meta is None:
            continue
        summary = _SUMMARY_RE.search(head)
        projects.append({"name": name[:-3], "repo": meta["repo"], "path": meta["path"],
                         "summary": summary.group(1) if summary else ""})
    return projects


def _page_summaries(vault: Path) -> list:
    """Every wiki page as {name, summary}, the summary being its own `**Summary**:`
    line ("" if it somehow lacks one). Head-reads each page like _list_lenses, so
    the whole vault costs one bounded read apiece."""
    rows = []
    for name in _list_wiki_pages(vault)["pages"]:
        try:
            head = (vault / "wiki" / name).read_text(encoding="utf-8")[:_HEAD_CHARS]
        except OSError:
            continue
        match = _SUMMARY_RE.search(head)
        rows.append({"name": name[:-3], "summary": match.group(1) if match else ""})
    return rows


def _split_sections(text: str) -> list[tuple[str, str]]:
    """(heading, section text including its heading) for each H2, in page order.
    Empty when the page has no H2s at all — which is why sections are a fallback
    for the pages that overflow, not the way every page is read: local-llm-agent.md
    is 9.8KB of prose with no headings to ask for."""
    heads = [(m.group(1).strip(), m.start()) for m in _H2_RE.finditer(text)]
    return [
        (name, text[start: heads[i + 1][1] if i + 1 < len(heads) else len(text)].rstrip())
        for i, (name, start) in enumerate(heads)
    ]


def _match_section(sections: list, wanted: str) -> tuple[str, str] | None:
    """The section whose heading best matches `wanted`; None if nothing is close.

    Deliberately forgiving. The model is re-typing a heading it read in a trim
    notice — "Product Theory and SVPG Alignment" is meaningful English, not an
    opaque id, but it's still 33 characters to reproduce, and an exact-match
    lookup would turn a dropped '&' into a dead end. Tries exact, then either
    direction of substring, then best word overlap."""
    want = wanted.strip().lower()
    for heading, body in sections:
        if heading.lower() == want:
            return heading, body

    # Longest match wins, not first: "Overview" is a substring of half the
    # phrasings a model might type, and page order would hand it back ahead of
    # the specific section actually asked for.
    hits = [(h, b) for h, b in sections if want in h.lower() or h.lower() in want]
    if hits:
        return max(hits, key=lambda hb: len(hb[0]))

    # Last resort. Words shorter than 4 chars are excluded because headings here
    # are English phrases: one shared "and" would otherwise be a match.
    terms = {t for t in _TERM_RE.findall(want) if len(t) >= 4}
    best, best_score = None, 0
    for heading, body in sections:
        score = len(terms & {t for t in _TERM_RE.findall(heading.lower()) if len(t) >= 4})
        if score > best_score:
            best, best_score = (heading, body), score
    return best


def _fit_page(text: str, budget: int = MAX_PAGE_CHARS) -> str:
    """A page trimmed to `budget` chars, cutting the part that matters least and
    saying what it cut.

    Blind truncation on a wiki page is worse than it looks. The `## Related
    pages` [[link]] block is always last, so a tail cut takes the page's
    navigation before it takes any prose; this keeps that block and cuts the
    body above it instead. It drops the `**Sources**:` line first — provenance
    for the ingest, never an answer to a question.

    The notice matters as much as the trim. Handed a silently cut page, the
    model treats what it got as the whole page: asked how AgentOS relates to
    SVPG, Wren read a truncated agentos.md and reported that SVPG was "not
    explicitly in the wiki" — from a page with a section on it, in the 58% she
    never saw. So the notice names the dropped sections and tells her not to
    make that claim. Reserve is generous because the names vary in length; a
    slight overshoot is harmless against loop.py's larger backstop."""
    text = _SOURCES_LINE_RE.sub("", text, count=1)
    if len(text) <= budget:
        return text

    related = _RELATED_RE.search(text)
    tail = related.group(0).strip() if related else ""
    body = text[: related.start()] if related else text

    kept = body[: max(0, budget - len(tail) - _NOTICE_RESERVE)]

    # A section counts as unread unless it ENDS inside the kept text. Testing
    # whether its heading survived isn't enough: the cut usually lands mid-section,
    # leaving the heading visible and most of its content gone — which would
    # report the section as read and reintroduce the false negative this notice
    # exists to prevent.
    heads = [(m.group(1), m.start()) for m in _H2_RE.finditer(body)]
    ends = [heads[i + 1][1] if i + 1 < len(heads) else len(body) for i in range(len(heads))]
    dropped = [name for (name, _), end in zip(heads, ends) if end > len(kept)]

    notice = (
        f"\n\n[Only the first {len(kept)} of {len(body)} characters of this page "
        "are shown; the rest did not fit."
        + (f" Not shown in full: {', '.join(dropped)}. Call read_wiki_page again "
           "with the section argument set to one of those headings to read it in "
           "full." if dropped else "")
        + (" The link list below is complete." if tail else "")
        + " Do NOT say this page or the wiki lacks something — you have not read "
        "all of it.]\n\n"
    )
    return kept + notice + tail


def _search_pages(vault: Path, query: str) -> list:
    """Pages whose name or summary matches `query`, as {name, summary} rows,
    best match first.

    Terms are matched as substrings, not as whole words: page names are slugs
    ('fractional-product-leadership'), so a term has to be able to match inside
    one. A name hit scores double a summary hit — the page named for a topic is
    more often the topic's page than one that merely mentions it. Ties break on
    the name so the ordering is stable run to run."""
    terms = _TERM_RE.findall(query.lower())
    if not terms:
        return []
    scored = []
    for row in _page_summaries(vault):
        name, summary = row["name"].lower(), row["summary"].lower()
        score = sum(2 for t in terms if t in name) + sum(1 for t in terms if t in summary)
        if score:
            scored.append((-score, row["name"], row))
    scored.sort()
    return [row for _, _, row in scored]


# --- model-facing tools (no vault argument; read the configured vault) ---

def read_wiki_index() -> dict:
    """The raw index.md. Not a registered tool — it's 62KB against an 8000-char
    tool-result cap (see the module docstring). Kept for the CLI, where a human
    reading the whole index is the point."""
    vault, err = _require_vault()
    return err or _read_index(vault)


def list_wiki_pages() -> dict:
    """Every concept-page filename. Not a registered tool either — 390 names is
    9.7KB, over the same cap. It exists for chat/insights.py, which counts and
    lists pages for the dashboard and has no such budget."""
    vault, err = _require_vault()
    return err or _list_wiki_pages(vault)


def search_wiki(query: str) -> dict:
    """Wiki pages matching `query`, as {name, summary} rows the model can pick a
    read_wiki_page target from. Capped (see MAX_SEARCH_RESULTS); when the cap
    bites, `truncated` says so in the result rather than dropping matches
    silently, so the model can narrow instead of assuming it saw everything."""
    if not query or not query.strip():
        return {"error": "query must not be empty"}
    vault, err = _require_vault()
    if err:
        return err
    matches = _search_pages(vault, query.strip())
    kept, total = [], 0
    for row in matches[:MAX_SEARCH_RESULTS]:
        kept.append(row)
        total += len(row["name"]) + len(row["summary"])
        if total > MAX_SEARCH_CHARS:
            break
    result = {"matches": kept}
    if len(kept) < len(matches):
        result["truncated"] = (
            f"showing the {len(kept)} best of {len(matches)} matching pages — "
            "use a narrower query to see the rest"
        )
    return result


def read_wiki_page(name: str, section: str | None = None) -> dict:
    if not name or not name.strip():
        return {"error": "name must not be empty"}
    vault, err = _require_vault()
    return err or _read_wiki_page(vault, name.strip(), section)


def page_summaries() -> dict:
    """Every wiki page with its one-line summary. Not a registered tool — it exists
    for tasks/daily_synthesis.py, which matches yesterday's activity against what a
    page is about rather than what it's named (matching on the filename alone can
    only ever find lexical identity)."""
    vault, err = _require_vault()
    return err or {"pages": _page_summaries(vault)}


def list_project_pages() -> dict:
    """The vault's project pages — those marked `project: true`. Not a
    registered tool: it exists for tasks/daily_synthesis.py, which merges a
    project's wiki page (the decisions and rationale) with the same project's
    scanned checkout (the current facts) into one anchor. Degrades to no
    projects if the vault is missing, so a misconfigured WIKI_VAULT_PATH costs
    the merge, not the run."""
    vault, err = _require_vault()
    if err:
        return {"projects": []}
    return {"projects": _list_project_pages(vault)}


def list_lenses() -> dict:
    """The evaluation lenses in the vault — pages marked `lens: true`. Degrades
    to no lenses if the vault is missing (this feeds the prompt build, which must
    never break on a misconfigured vault)."""
    vault, err = _require_vault()
    if err:
        return {"lenses": []}
    return {"lenses": _list_lenses(vault)}


def render_lenses_index(logger=None) -> str:
    """The capped 'name: description' block injected into the chat system prompt
    so Wren knows which pages are the user's standards lenses (and their exact
    names, to pass as evaluate_against's lens_page). "" when there are none.
    Mirrors skills.render_skills_index.

    A lens dropped by either cap is invisible to Wren, who then never offers it —
    a silent degrade, so it's logged at WARNING with the counts and the names
    (AGENTS.md). MAX_INDEX_CHARS is usually the cap that bites: descriptions run
    ~150-200 chars, so ~3 exhaust the budget well before MAX_INDEX_LENSES.
    Rendered per turn, so a persistent drop repeats every turn; log_inspector
    collapses identical warnings into a count rather than pushing each one."""
    lenses = list_lenses()["lenses"]
    if not lenses:
        return ""
    lines, total = [], 0
    for lens in lenses[:MAX_INDEX_LENSES]:
        line = f"- {lens['name']}: {lens['description']}" if lens["description"] else f"- {lens['name']}"
        if total + len(line) > MAX_INDEX_CHARS:
            break
        lines.append(line)
        total += len(line)
    if len(lines) < len(lenses) and logger:
        # Only ever a truncated prefix, so the dropped ones are the tail.
        dropped = [lens["name"] for lens in lenses[len(lines):]]
        cause = (f"the {MAX_INDEX_CHARS}-char budget"
                 if len(lines) < min(len(lenses), MAX_INDEX_LENSES)
                 else f"the {MAX_INDEX_LENSES}-lens cap")
        logger.warning(
            f"lens index truncated by {cause}: {len(lines)} of {len(lenses)} lenses "
            f"in the prompt ({total} chars), dropped {', '.join(dropped)} — dropped "
            f"lenses are invisible in chat; shorten the frontmatter descriptions"
        )
    if not lines:
        return ""
    return (
        f"Evaluation lenses ({_NAME}'s own standards pages). When they ask to evaluate, "
        "critique, or review something against their standards, principles, or "
        "philosophy, call evaluate_against with the matching lens_page:\n"
        + "\n".join(lines)
    )


# Wording follows the catalogue-tool rule in AGENTS.md: a "what exists?" tool has
# to state outright that the answer is not in the model's head, or pretraining
# supplies a plausible page name and the model skips the call. The wiki is the
# worst case for that — its topics (agents, RAG, product management) are exactly
# what a model can invent confident-sounding page names about.
SEARCH_WIKI_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_wiki",
        "description": (
            f"Search {_NAME}'s personal learnings wiki by topic and get back each "
            "matching page name with its one-line summary. This is the ONLY way to "
            "find out what is in the wiki: it holds hundreds of pages written from "
            f"{_NAME}'s own notes and reviews, and you do not know what any of them "
            "are. Only the pages this tool returns exist. Never name, describe, or "
            "read a wiki page that did not come back from a search. Call this first, "
            "then call read_wiki_page on the page you want. If it returns no matches, "
            "say the wiki has nothing on that topic — do not guess a page name and do "
            "not answer from your own knowledge as if it came from the wiki."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Topic words, matched against page names and summaries, e.g. "
                        "'fractional product leadership'. Prefer two or three words."
                    ),
                },
            },
            "required": ["query"],
        },
    },
}

READ_WIKI_PAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_wiki_page",
        "description": (
            f"Read one concept page from {_NAME}'s learnings wiki, by a name search_wiki "
            "returned. Cite the page name in your answer. A few pages are too long to "
            "return whole; those come back with a note naming the sections that were "
            "cut. When the answer you need is in one of those, call this again with "
            "that heading as `section` to read that part in full."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Page name, e.g. 'speakers-bureau' (with or without .md)."},
                "section": {
                    "type": "string",
                    "description": (
                        "Optional. A section heading from the page, e.g. 'Product Theory "
                        "and SVPG Alignment'. Returns just that section. Use only a "
                        "heading the page itself named; omit to read the page."
                    ),
                },
            },
            "required": ["name"],
        },
    },
}

WIKI_TOOL_SCHEMAS = [
    SEARCH_WIKI_SCHEMA,
    READ_WIKI_PAGE_SCHEMA,
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("read-index")
    sub.add_parser("list-pages")
    p_search = sub.add_parser("search")
    p_search.add_argument("query")
    p_page = sub.add_parser("read-page")
    p_page.add_argument("name")
    p_page.add_argument("--section", default=None)
    args = parser.parse_args()

    if args.cmd == "read-index":
        result = read_wiki_index()
    elif args.cmd == "list-pages":
        result = list_wiki_pages()
    elif args.cmd == "search":
        result = search_wiki(args.query)
    else:
        result = read_wiki_page(args.name, args.section)

    return print_result(result)


if __name__ == "__main__":
    sys.exit(main())
