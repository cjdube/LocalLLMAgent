"""Read-only access to Craig's personal learnings wiki (an Obsidian vault) so
Wren can answer "what did I decide about X" from his own notes.

The vault at WIKI_VAULT_PATH is built and maintained by the sibling
ObsidianWikiAgent project: raw/ holds the weekly reviews, wiki/ holds the
LLM-summarized, cross-linked concept pages plus an index.md table of contents.
These tools let the agent loop navigate that the way ObsidianWikiAgent's own
query CLI does: read the index, open the relevant page(s), answer.

Vault layout (see ObsidianWikiAgent):
    <vault>/wiki/index.md   -- table of contents the model reads first
    <vault>/wiki/*.md       -- concept pages (excluding index.md, log.md)
    <vault>/raw/*.md        -- raw weekly reviews (may cover weeks not yet in wiki/)

The vault lives on an external drive, so a missing vault dir is surfaced as an
error (like learnings_file.py) rather than raising. Functions read from a single
configured vault; the internal helpers take vault_path explicitly so pointing at
another vault later is a config change, not a rewrite.

Usage:
    python -m agent.tools.wiki read-index
    python -m agent.tools.wiki list-pages
    python -m agent.tools.wiki read-page speakers-bureau
    python -m agent.tools.wiki list-reviews
    python -m agent.tools.wiki read-review Strategic-Weekly-Review-2026-07-05.md
"""

import argparse
import os
import sys
from pathlib import Path

from agent.tools._http import load_env, print_result

load_env()

DEFAULT_WIKI_VAULT = "/Volumes/T7/Obsidian/learnings"


def _vault() -> Path:
    return Path(os.getenv("WIKI_VAULT_PATH", DEFAULT_WIKI_VAULT))


def _require_vault() -> tuple[Path | None, dict | None]:
    """Return (vault, None) or (None, error) if the vault dir is missing —
    e.g. the external drive isn't mounted. Mirrors learnings_file.py so Wren
    degrades gracefully instead of raising."""
    vault = _vault()
    if not vault.exists():
        return None, {"error": f"learnings vault not found (drive not mounted?): {vault}"}
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


def _list_raw_files(vault: Path) -> dict:
    raw_dir = vault / "raw"
    if not raw_dir.exists():
        return {"files": []}
    files = sorted(
        p.name for p in raw_dir.iterdir()
        if p.is_file() and p.suffix == ".md" and not p.name.startswith(".")
    )
    return {"files": files}


def _read_raw_file(vault: Path, filename: str) -> dict:
    try:
        path = _safe_child(vault / "raw", filename)
    except ValueError as e:
        return {"error": str(e)}
    if not path.is_file():
        return {"error": f"weekly review '{filename}' not found"}
    return {"content": path.read_text(encoding="utf-8", errors="replace")}


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


def list_weekly_reviews() -> dict:
    vault, err = _require_vault()
    return err or _list_raw_files(vault)


def read_weekly_review(filename: str) -> dict:
    if not filename or not filename.strip():
        return {"error": "filename must not be empty"}
    vault, err = _require_vault()
    return err or _read_raw_file(vault, filename.strip())


READ_WIKI_INDEX_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_wiki_index",
        "description": (
            "Read the table of contents (index.md) of Craig's personal learnings "
            "wiki — the concept pages built from his weekly reviews. Start here to "
            "see what topics exist before reading a page."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

LIST_WIKI_PAGES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_wiki_pages",
        "description": "List the concept-page filenames in Craig's learnings wiki (excludes index.md and log.md).",
        "parameters": {"type": "object", "properties": {}},
    },
}

READ_WIKI_PAGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_wiki_page",
        "description": "Read one concept page from Craig's learnings wiki. Cite the page name in your answer.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Page name, e.g. 'speakers-bureau' (with or without .md)."},
            },
            "required": ["name"],
        },
    },
}

LIST_WEEKLY_REVIEWS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_weekly_reviews",
        "description": (
            "List Craig's raw weekly review filenames (Strategic-Weekly-Review-<date>.md). "
            "Use these for a recent week that may not be summarized into the wiki yet."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

READ_WEEKLY_REVIEW_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_weekly_review",
        "description": "Read one raw weekly review file by filename (from list_weekly_reviews).",
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Filename, e.g. 'Strategic-Weekly-Review-2026-07-05.md'."},
            },
            "required": ["filename"],
        },
    },
}

WIKI_TOOL_SCHEMAS = [
    READ_WIKI_INDEX_SCHEMA,
    LIST_WIKI_PAGES_SCHEMA,
    READ_WIKI_PAGE_SCHEMA,
    LIST_WEEKLY_REVIEWS_SCHEMA,
    READ_WEEKLY_REVIEW_SCHEMA,
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("read-index")
    sub.add_parser("list-pages")
    p_page = sub.add_parser("read-page")
    p_page.add_argument("name")
    sub.add_parser("list-reviews")
    p_review = sub.add_parser("read-review")
    p_review.add_argument("filename")
    args = parser.parse_args()

    if args.cmd == "read-index":
        result = read_wiki_index()
    elif args.cmd == "list-pages":
        result = list_wiki_pages()
    elif args.cmd == "read-page":
        result = read_wiki_page(args.name)
    elif args.cmd == "list-reviews":
        result = list_weekly_reviews()
    else:
        result = read_weekly_review(args.filename)

    return print_result(result)


if __name__ == "__main__":
    sys.exit(main())
