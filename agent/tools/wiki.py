"""Read-only access to the user's personal learnings wiki (an Obsidian vault) so
Wren can answer "what did I decide about X" from their own notes.

The vault at WIKI_VAULT_PATH is built and maintained by the sibling
ObsidianWikiAgent project. These tools let the agent loop navigate it the way
ObsidianWikiAgent's own query CLI does: read the index, open the relevant
page(s), answer.

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
    python -m agent.tools.wiki read-index
    python -m agent.tools.wiki list-pages
    python -m agent.tools.wiki read-page speakers-bureau
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

DEFAULT_WIKI_VAULT = str(Path.home() / "Documents" / "llm-wiki-learnings")

# A "lens" is an ordinary wiki page that opts in as one of the user's standards
# rubrics by declaring `lens: true` in its YAML frontmatter — the thing
# evaluate_against judges a target against. Nothing else distinguishes it from a
# concept page, so the marker is how Wren tells lenses from the 180-odd other
# notes. Keep the injected index cheap (same budget reasoning as the skills
# index): the chat prompt already crowds num_ctx.
MAX_INDEX_LENSES = 8
MAX_INDEX_CHARS = 600

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


def _read_wiki_page(vault: Path, name: str) -> dict:
    filename = name if name.endswith(".md") else f"{name}.md"
    try:
        path = _safe_child(vault / "wiki", filename)
    except ValueError as e:
        return {"error": str(e)}
    if not path.is_file():
        return {"error": f"wiki page '{name}' not found"}
    return {"content": path.read_text(encoding="utf-8")}


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


def _list_projects(vault: Path) -> list:
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


# --- model-facing tools (no vault argument; read the configured vault) ---

def read_wiki_index() -> dict:
    vault, err = _require_vault()
    return err or _read_index(vault)


def list_wiki_pages() -> dict:
    vault, err = _require_vault()
    return err or _list_wiki_pages(vault)


def read_wiki_page(name: str) -> dict:
    if not name or not name.strip():
        return {"error": "name must not be empty"}
    vault, err = _require_vault()
    return err or _read_wiki_page(vault, name.strip())


def page_summaries() -> dict:
    """Every wiki page with its one-line summary. Not a registered tool — it exists
    for tasks/daily_synthesis.py, which matches yesterday's activity against what a
    page is about rather than what it's named (matching on the filename alone can
    only ever find lexical identity)."""
    vault, err = _require_vault()
    return err or {"pages": _page_summaries(vault)}


def list_projects() -> dict:
    """The vault's project pages — those marked `project: true`. Not a
    registered tool: it exists for tasks/daily_synthesis.py, which merges a
    project's wiki page (the decisions and rationale) with the same project's
    scanned checkout (the current facts) into one anchor. Degrades to no
    projects if the vault is missing, so a misconfigured WIKI_VAULT_PATH costs
    the merge, not the run."""
    vault, err = _require_vault()
    if err:
        return {"projects": []}
    return {"projects": _list_projects(vault)}


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
    (CLAUDE.md). MAX_INDEX_CHARS is usually the cap that bites: descriptions run
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


READ_WIKI_INDEX_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_wiki_index",
        "description": (
            f"Read the table of contents (index.md) of {_NAME}'s personal learnings "
            "wiki — the concept pages built from their weekly reviews. Start here to "
            "see what topics exist before reading a page."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

LIST_WIKI_PAGES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_wiki_pages",
        "description": f"List the concept-page filenames in {_NAME}'s learnings wiki (excludes index.md and log.md).",
        "parameters": {"type": "object", "properties": {}},
    },
}

READ_WIKI_PAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_wiki_page",
        "description": f"Read one concept page from {_NAME}'s learnings wiki. Cite the page name in your answer.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Page name, e.g. 'speakers-bureau' (with or without .md)."},
            },
            "required": ["name"],
        },
    },
}

WIKI_TOOL_SCHEMAS = [
    READ_WIKI_INDEX_SCHEMA,
    LIST_WIKI_PAGES_SCHEMA,
    READ_WIKI_PAGE_SCHEMA,
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("read-index")
    sub.add_parser("list-pages")
    p_page = sub.add_parser("read-page")
    p_page.add_argument("name")
    args = parser.parse_args()

    if args.cmd == "read-index":
        result = read_wiki_index()
    elif args.cmd == "list-pages":
        result = list_wiki_pages()
    else:
        result = read_wiki_page(args.name)

    return print_result(result)


if __name__ == "__main__":
    sys.exit(main())
