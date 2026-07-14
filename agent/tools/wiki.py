"""Read-only access to Craig's personal learnings wiki (an Obsidian vault) so
Wren can answer "what did I decide about X" from his own notes.

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

The vault lives on an external drive, so a missing vault dir is surfaced as an
error (like learnings_file.py) rather than raising. Functions read from a single
configured vault; the internal helpers take vault_path explicitly so pointing at
another vault later is a config change, not a rewrite.

Usage:
    python -m agent.tools.wiki read-index
    python -m agent.tools.wiki list-pages
    python -m agent.tools.wiki read-page speakers-bureau
"""

import argparse
import os
import sys
from pathlib import Path

from agent.tools._http import load_env, print_result

load_env()

DEFAULT_WIKI_VAULT = str(Path.home() / "Documents" / "llm-wiki-learnings")


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
