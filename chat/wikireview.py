"""The /wiki/review data: every vault page, placed in one group, plus saved marks.

No Flask and no model. The group list is stored data in config/wiki_review.json;
this module places the vault's current pages into it on every read, so a page
that was cut drops out and a new page lands in "Unsorted" with nothing to run.

The marks are an opinion about the vault, not an edit to it: nothing here writes
under the vault. See docs/wiki-review.md.
"""

import re
from pathlib import Path

from agent.store import atomic_write_json, load_json, locked
from agent.tools.wiki import _SUMMARY_RE, _list_wiki_pages, _require_vault
from chat.wikigraph import _UPDATED_RE, link_targets

_STORE_PATH = Path(__file__).resolve().parent.parent / "config" / "wiki_review.json"

MAX_SUMMARY_CHARS = 220  # wikigraph's 140 is too short to judge a page from
UNSORTED_ID = "unsorted"
VERDICTS = ("keep", "unsure", "cut")
PAGE_MARKS = ("keep", "cut")

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATE_AND_AFTER_RE = re.compile(r"-?\d{4}-\d{2}-\d{2}.*")
_SOURCES_RE = re.compile(r"^\*\*Sources\*\*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_SOURCE_FILE_RE = re.compile(r"[^,\s][^,]*?\.md")
_WIKI_LINK_RE = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]")


def _load() -> dict:
    return load_json(_STORE_PATH, {"groups": [], "marks": {}, "sorted_at": ""})


def _page_facts(vault: Path) -> dict:
    """{name: [summary, words, sources, inbound, newest date]} for every page."""
    texts = {}
    for filename in _list_wiki_pages(vault)["pages"]:
        try:
            text = (vault / "wiki" / filename).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        texts[filename[: -len(".md")]] = text

    # Case-folded because Obsidian resolves links that way. A set per page, so a
    # page that links twice still counts as one page that depends on the target.
    inbound: dict[str, int] = {}
    for name, text in texts.items():
        for target in {t.lower() for t in link_targets(text)} - {name.lower()}:
            inbound[target] = inbound.get(target, 0) + 1

    facts = {}
    for name, text in texts.items():
        summary = _SUMMARY_RE.search(text)
        summary = _WIKI_LINK_RE.sub(r"\1", summary.group(1)) if summary else ""
        sources = _SOURCES_RE.search(text)
        sources = _SOURCE_FILE_RE.findall(sources.group(1)) if sources else []
        dates = [d for source in sources for d in _DATE_RE.findall(source)]
        if not dates:
            updated = _UPDATED_RE.search(text)
            dates = _DATE_RE.findall(updated.group(1)) if updated else []
        facts[name] = [
            summary[:MAX_SUMMARY_CHARS],
            len(text.split()),
            len(sources),
            inbound.get(name.lower(), 0),
            max(dates, default=""),
        ]
    return facts


def _is_dated_under(name: str, prefix: str) -> bool:
    return bool(_DATE_RE.search(name)) and _DATE_AND_AFTER_RE.sub("", name).startswith(prefix)


def _place(stored_groups: list, names) -> list:
    """Each page in exactly one group: first stored group to claim it wins, and
    whatever nobody claims goes in Unsorted. A name no longer in the vault drops
    out here, and so does a group left with no pages."""
    remaining = set(names)
    placed = []
    for group in stored_groups:
        if "prefix" in group:
            wanted = sorted(n for n in remaining if _is_dated_under(n, group["prefix"]))
        else:
            wanted = [n for n in group["pages"] if n in remaining]
        remaining.difference_update(wanted)
        if wanted:
            placed.append({key: group[key] for key in ("id", "family", "title", "line")}
                          | {"pages": wanted})
    if remaining:
        placed.append({
            "id": UNSORTED_ID,
            "family": "Not sorted yet",
            "title": "Unsorted",
            "line": "Pages that no group holds yet.",
            "pages": sorted(remaining),
        })
    return placed


def review() -> dict:
    """{groups, pages, marks, sorted_at}, or {"error": ...} if the vault is missing."""
    vault, err = _require_vault()
    if err:
        return err
    facts = _page_facts(vault)
    store = _load()
    groups = _place(store["groups"], facts)

    # Only overrides for pages still in that group, or the "set by hand" count
    # on the page would include pages it cannot show.
    marks = {}
    for group in groups:
        mark = store["marks"].get(group["id"])
        if mark:
            members = set(group["pages"])
            marks[group["id"]] = {
                "verdict": mark["verdict"],
                "pages": {n: v for n, v in mark["pages"].items() if n in members},
            }
    return {"groups": groups, "pages": facts, "marks": marks, "sorted_at": store["sorted_at"]}


def set_mark(group_id: str, verdict, pages) -> None:
    """Save one group's marks. ValueError for a bad value; LookupError for a
    group or page that does not exist in the vault as it stands now."""
    if verdict is not None and verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS} or null")
    if not isinstance(pages, dict) or any(v not in PAGE_MARKS for v in pages.values()):
        raise ValueError(f"pages must map a page name to one of {PAGE_MARKS}")

    vault, err = _require_vault()
    if err:
        raise LookupError(err["error"])
    # Only the file list, not the facts: this runs on every tap.
    names = [f[: -len(".md")] for f in _list_wiki_pages(vault)["pages"]]

    with locked(_STORE_PATH):
        store = _load()
        group = next((g for g in _place(store["groups"], names) if g["id"] == group_id), None)
        if group is None:
            raise LookupError(f"unknown group '{group_id}'")
        unknown = sorted(set(pages) - set(group["pages"]))
        if unknown:
            raise LookupError(f"not in group '{group_id}': {', '.join(unknown)}")
        if verdict is None and not pages:
            store["marks"].pop(group_id, None)
        else:
            store["marks"][group_id] = {"verdict": verdict, "pages": pages}
        atomic_write_json(_STORE_PATH, store)
