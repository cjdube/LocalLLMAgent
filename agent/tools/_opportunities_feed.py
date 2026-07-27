"""Scout/digest-facing CRUD over the opportunity store.

The chat-facing tool surface (list/update/watch/unwatch + schemas) lives in
agent/tools/opportunities.py; these are the functions the daily scout
(tasks/opportunity_digest.py) and the research pipeline call to insert, score,
re-signal, and retire items. They're re-exported from opportunities.py, so
callers keep using `opportunities.insert_new_items(...)` etc. unchanged.

Both halves share one store (config/opportunities.json). This module reaches it
through opportunities.py's primitives — _locked() (the lock, which resolves
_STORE_PATH at call time so test isolation still holds), _load, _save, _now —
rather than binding _STORE_PATH itself. Private (`_`-prefixed) because it is an
internal split of opportunities.py, imported only from there."""

from datetime import datetime
from uuid import uuid4

from agent.tools.opportunities import _load, _locked, _now, _save


def insert_new_items(candidates: list) -> list:
    """Insert candidate items not already in the store (dedupe by the stable
    natural id each poller builds, e.g. 'edgar:<accession>'). Returns the
    newly inserted items — the ones the digest should score and report."""
    now = _now()
    inserted = []
    with _locked():
        data = _load()
        known = {i["id"] for i in data["items"]}
        for c in candidates:
            if c["id"] in known:
                continue
            item = {
                "score": None, "angle": None, "posted_at": None, "title": None,
                "url": None, "location": None,
                **c,
                "status": "new", "first_seen": now, "updated": now,
            }
            data["items"].append(item)
            known.add(item["id"])
            inserted.append(item)
        if inserted:
            _save(data)
    return inserted


def _parse_ts(value: str) -> datetime | None:
    """Parse an ISO timestamp (or bare date) into naive local time, tolerating
    the aware/naive mix in the store: posted_at comes from external APIs
    (usually tz-aware), first_seen from _now() (naive local)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def flip_stalled(open_postings: dict, stalled_days: int, now: datetime | None = None) -> list:
    """Re-signal watched openings that have been open too long. open_postings
    is {item_id: posted_at_or_None} for every leadership posting currently
    open on a watched board; an item's age comes from that posted date when
    the board provides one, else from when the scout first saw it. Only
    'hiring' items flip — so each flips exactly once, since the age check
    would otherwise match on every later run — and dismissed ones stay
    dismissed. Flipped items return to status 'new' and shed any prior
    score/angle so the next digest reports the stall and re-scores it under
    the stronger signal. Returns the flipped items."""
    now = now or datetime.now()
    flipped = []
    with _locked():
        data = _load()
        for item in data["items"]:
            if item["id"] not in open_postings or item["signal"] != "hiring":
                continue
            if item["status"] == "dismissed":
                continue
            since = _parse_ts(open_postings[item["id"]]) or _parse_ts(item["first_seen"])
            if since is None or (now - since).days < stalled_days:
                continue
            item["signal"] = "stalled_search"
            item["status"] = "new"
            item.pop("score", None)
            item.pop("angle", None)
            item["updated"] = _now()
            flipped.append(item)
        if flipped:
            _save(data)
    return flipped


def close_missing(open_ids: set, polled_boards: list, now: str | None = None) -> list:
    """Retire watched openings that have vanished from their board.

    open_ids is every ATS item id seen in this run's poll; polled_boards is the
    "<ats>:<slug>" of every board that answered *without error*. An item is only
    considered when its own board is in that list, so a timed-out board — or one
    the user just unwatched — can't be read as "every role there was filled".

    A closure isn't a judgement, so it never becomes 'dismissed': items get
    their own terminal 'closed' status (and age out on the same 30-day clock).
    'interested' is the exception — the user may already have reached out, so the
    item keeps its status and just carries closed_at, which the page badges as
    "no longer listed". Marked once: an item that already has closed_at is left
    alone rather than having its timestamps bumped every week. Returns the
    items that changed."""
    boards = set(polled_boards)
    stamp = now or _now()
    changed = []
    with _locked():
        data = _load()
        for item in data["items"]:
            if item.get("source") != "ats" or item.get("closed_at"):
                continue
            if item["id"].rsplit(":", 1)[0] not in boards or item["id"] in open_ids:
                continue
            if item["status"] in ("dismissed", "closed"):
                continue
            if item["status"] != "interested":
                item["status"] = "closed"
            item["closed_at"] = stamp
            item["updated"] = stamp
            changed.append(item)
        if changed:
            _save(data)
    return changed


def get_item(item_id: str) -> dict | None:
    with _locked():
        return next((i for i in _load()["items"] if i["id"] == item_id), None)


def set_research(item_id: str, research: dict) -> bool:
    """Attach a research payload ({"status": "pending"|"done"|"failed", ...})
    to an item. Returns False if the item doesn't exist."""
    with _locked():
        data = _load()
        item = next((i for i in data["items"] if i["id"] == item_id), None)
        if item is None:
            return False
        item["research"] = research
        item["updated"] = _now()
        _save(data)
    return True


def all_items(limit: int = 200) -> list:
    """Full item dicts, newest first — for the dashboard's /opportunities page
    (page-sized cap), not the model: list_opportunities is the
    context-bounded view chat gets."""
    with _locked():
        items = _load()["items"]
    return sorted(items, key=lambda i: i["first_seen"], reverse=True)[:limit]


def pending_new_items() -> list:
    """Every item awaiting its first digest report (status 'new'), oldest
    first — uncapped, unlike list_opportunities, since this feeds the digest
    builder rather than the model's context."""
    with _locked():
        items = [i for i in _load()["items"] if i["status"] == "new"]
    return sorted(items, key=lambda i: i["first_seen"])


def record_scores(scores: dict) -> None:
    """Persist model scores/angles: {item_id: (score, angle)}."""
    with _locked():
        data = _load()
        changed = False
        for item in data["items"]:
            if item["id"] in scores:
                item["score"], item["angle"] = scores[item["id"]]
                item["updated"] = _now()
                changed = True
        if changed:
            _save(data)


def mark_digested(item_ids: list) -> None:
    """Move reported items out of 'new' after a successful digest send, so the
    next run doesn't re-report them."""
    ids = set(item_ids)
    with _locked():
        data = _load()
        changed = False
        for item in data["items"]:
            if item["id"] in ids and item["status"] == "new":
                item["status"] = "digested"
                item["updated"] = _now()
                changed = True
        if changed:
            _save(data)
