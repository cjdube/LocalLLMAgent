"""Opportunity signal store — companies showing a pain a fractional product
operator can solve, gathered daily by tasks/opportunity_digest.py.

Three signals, all from free ToS-clean sources (see CLAUDE.md's data sourcing
policy): "funded" (new SEC Form D filings), "hiring" (product/eng leadership
openings at watched companies, plus HN Who-is-hiring posts), and
"stalled_search" (a watched leadership opening still unfilled after
OPP_STALLED_DAYS — flipped once, when it crosses the threshold).

The chat model manages the watchlist and item statuses through the tools
below; the digest task inserts/scores items. State lives in
config/opportunities.json, written atomically under a cross-process file lock
(agent/store.py) so the Flask chat server and the scheduled task never read a
half-written file or clobber each other's updates.

Usage:
    python -m agent.tools.opportunities --list
    python -m agent.tools.opportunities --watchlist
    python -m agent.tools.opportunities --watch "Acme" greenhouse acme
    python -m agent.tools.opportunities --unwatch <id-or-company>
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from agent.store import atomic_write_json, load_json, locked

_ROOT = Path(__file__).resolve().parent.parent.parent
_STORE_PATH = _ROOT / "config" / "opportunities.json"

_ATS_KINDS = ("greenhouse", "lever", "ashby", "icims")

# Craig acts on an item by marking it; everything else is lifecycle the digest
# task manages ("new" -> scored + emailed -> "digested").
_SETTABLE_STATUSES = ("interested", "dismissed")

# Keep the store bounded: it grows from daily polling, and list_opportunities
# feeds the model's context. Items Craig has dealt with (dismissed) or that
# were reported and never acted on (digested) fall off after the window;
# "interested" is his live pipeline and "new" is pre-digest, so both are kept.
_PRUNE_AFTER_S = 30 * 24 * 3600
_LIST_LIMIT = 20


LIST_OPPORTUNITIES_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_opportunities",
        "description": "List fractional-work opportunity signals Wren's daily scout has gathered "
        "(funded companies, leadership openings, stalled exec searches), newest first. Use when "
        "Craig asks about opportunities, leads, or the pipeline. Optionally filter by status "
        "(new, digested, interested, dismissed) or signal (funded, hiring, stalled_search).",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Only items with this status."},
                "signal": {"type": "string", "description": "Only items with this signal."},
            },
            "required": [],
        },
    },
}

UPDATE_OPPORTUNITY_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_opportunity",
        "description": "Mark an opportunity 'interested' (Craig wants to pursue it — kept "
        "indefinitely) or 'dismissed' (not a fit — ages out). Get the id from "
        "list_opportunities first.",
        "parameters": {
            "type": "object",
            "properties": {
                "opportunity_id": {"type": "string", "description": "The id of the opportunity."},
                "status": {"type": "string", "enum": list(_SETTABLE_STATUSES)},
            },
            "required": ["opportunity_id", "status"],
        },
    },
}

WATCH_COMPANY_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "watch_company",
        "description": "Add a company to the opportunity watchlist: the daily scout polls its "
        "public job board for leadership openings and flags stalled searches. The slug is the "
        "identifier in its job-board URL, e.g. boards.greenhouse.io/<slug>, jobs.lever.co/<slug>, "
        "jobs.ashbyhq.com/<slug>, or <slug>.icims.com.",
        "parameters": {
            "type": "object",
            "properties": {
                "company": {"type": "string", "description": "Company name, e.g. 'Acme'."},
                "ats": {"type": "string", "enum": list(_ATS_KINDS),
                        "description": "Which applicant-tracking system hosts its job board."},
                "slug": {"type": "string", "description": "The company's board slug in that ATS."},
            },
            "required": ["company", "ats", "slug"],
        },
    },
}

UNWATCH_COMPANY_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "unwatch_company",
        "description": "Remove a company from the opportunity watchlist, by its watchlist id or "
        "its company name.",
        "parameters": {
            "type": "object",
            "properties": {
                "watch_id": {"type": "string",
                             "description": "Watchlist id or company name to remove."},
            },
            "required": ["watch_id"],
        },
    },
}

OPPORTUNITY_TOOL_SCHEMAS = (
    LIST_OPPORTUNITIES_TOOL_SCHEMA,
    UPDATE_OPPORTUNITY_TOOL_SCHEMA,
    WATCH_COMPANY_TOOL_SCHEMA,
    UNWATCH_COMPANY_TOOL_SCHEMA,
)


def _load() -> dict:
    return load_json(_STORE_PATH, {"watchlist": [], "items": []})


def _locked():
    """The store's cross-process lock, resolving _STORE_PATH at call time.

    The scout-facing CRUD lives in _opportunities_feed.py but shares this one
    store; it acquires the lock through this helper rather than binding
    _STORE_PATH itself, so a test that monkeypatches opportunities._STORE_PATH
    (the single isolation point — see tests/conftest.py) still redirects it."""
    return locked(_STORE_PATH)


def _prune(data: dict, now: datetime | None = None) -> None:
    now = now or datetime.now()

    def keep(item: dict) -> bool:
        if item["status"] not in ("digested", "dismissed"):
            return True
        try:
            age = (now - datetime.fromisoformat(item["updated"])).total_seconds()
        except (ValueError, TypeError):
            return True  # unparseable timestamp: keep rather than guess
        return age <= _PRUNE_AFTER_S

    data["items"] = [i for i in data["items"] if keep(i)]


def _save(data: dict) -> None:
    _prune(data)
    atomic_write_json(_STORE_PATH, data)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---- chat tools -----------------------------------------------------------

def list_opportunities(status: str = None, signal: str = None) -> dict:
    with locked(_STORE_PATH):
        items = _load()["items"]
    if status:
        items = [i for i in items if i["status"] == status]
    if signal:
        items = [i for i in items if i["signal"] == signal]
    newest_first = sorted(items, key=lambda i: i["first_seen"], reverse=True)
    return {
        "count": len(items),
        # Capped: this listing lands in the model's context window.
        "opportunities": [
            {"id": i["id"], "signal": i["signal"], "status": i["status"],
             "company": i["company"], "title": i.get("title") or "",
             "score": i.get("score"), "angle": i.get("angle") or "",
             "url": i.get("url") or "", "first_seen": i["first_seen"]}
            for i in newest_first[:_LIST_LIMIT]
        ],
    }


def update_opportunity(opportunity_id: str, status: str) -> dict:
    if status not in _SETTABLE_STATUSES:
        return {"error": f"status must be one of {_SETTABLE_STATUSES}, got {status!r}"}
    with locked(_STORE_PATH):
        data = _load()
        item = next((i for i in data["items"] if i["id"] == opportunity_id), None)
        if item is None:
            return {"error": f"no opportunity with id {opportunity_id!r}"}
        item["status"] = status
        item["updated"] = _now()
        _save(data)
    return {"id": opportunity_id, "status": status, "company": item["company"]}


def watch_company(company: str, ats: str, slug: str) -> dict:
    company = (company or "").strip()
    ats = (ats or "").strip().lower()
    slug = (slug or "").strip()
    if not company or not slug:
        return {"error": "company and slug are both required"}
    if ats not in _ATS_KINDS:
        return {"error": f"ats must be one of {_ATS_KINDS}, got {ats!r}"}
    entry = {
        "id": uuid4().hex[:8],
        "company": company,
        "ats": ats,
        "slug": slug,
        "added_at": _now(),
    }
    with locked(_STORE_PATH):
        data = _load()
        dup = next(
            (w for w in data["watchlist"] if w["ats"] == ats and w["slug"] == slug), None
        )
        if dup:
            return {"error": f"already watching {dup['company']} ({ats}/{slug}), id {dup['id']}"}
        data["watchlist"].append(entry)
        _save(data)
    return {"id": entry["id"], "company": company, "ats": ats, "slug": slug,
            "note": "Watching — leadership openings on this board appear in the daily digest."}


def unwatch_company(watch_id: str) -> dict:
    needle = (watch_id or "").strip().lower()
    with locked(_STORE_PATH):
        data = _load()
        entry = next(
            (w for w in data["watchlist"]
             if w["id"] == watch_id or w["company"].lower() == needle),
            None,
        )
        if entry is None:
            return {"error": f"no watchlist entry matching {watch_id!r}"}
        data["watchlist"] = [w for w in data["watchlist"] if w["id"] != entry["id"]]
        _save(data)
    return {"removed": True, "id": entry["id"], "company": entry["company"]}


def get_watchlist() -> list:
    with locked(_STORE_PATH):
        return _load()["watchlist"]


# ---- digest-task-facing helpers -------------------------------------------
# The scout/digest CRUD (insert/score/re-signal/retire) lives in the private
# _opportunities_feed module — same store, different surface. Re-exported here
# so callers keep using opportunities.insert_new_items(...) etc. The import
# sits at the bottom, after the store primitives (_locked/_load/_save/_now)
# the feed module imports back, so the two modules load without a cycle.
from agent.tools._opportunities_feed import (  # noqa: E402
    all_items,
    flip_stalled,
    get_item,
    insert_new_items,
    mark_digested,
    pending_new_items,
    record_scores,
    set_research,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list stored opportunities")
    parser.add_argument("--watchlist", action="store_true", help="list watched companies")
    parser.add_argument("--watch", nargs=3, metavar=("COMPANY", "ATS", "SLUG"))
    parser.add_argument("--unwatch", metavar="ID_OR_COMPANY")
    args = parser.parse_args()
    if args.watch:
        print(json.dumps(watch_company(*args.watch), indent=2))
    elif args.unwatch:
        print(json.dumps(unwatch_company(args.unwatch), indent=2))
    elif args.watchlist:
        print(json.dumps({"watchlist": get_watchlist()}, indent=2))
    else:
        print(json.dumps(list_opportunities(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
