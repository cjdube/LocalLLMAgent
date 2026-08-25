"""Durable state for the Gmail watcher: the history watermark, the watch
expiry, and the seen-message set that makes redelivery a no-op.

Three separate hazards, one store, because the watcher has to commit all three
together (see commit() below):

1. **Pub/Sub is at-least-once.** The same notification arrives more than once —
   on a redelivery after a crash, and routinely, because Gmail publishes one
   notification per mailbox change and several changes can name the same
   message. Without `seen`, one email pushes to the phone two or three times.

2. **Pub/Sub does not guarantee order.** A notification carrying an older
   historyId can land after a newer one. Storing whichever arrived last would
   walk the watermark backwards and re-report mail already handled, so
   `history_id` only ever moves forward — see _newer().

3. **Gmail drops the watch after 7 days and says nothing.** `watch_expiration`
   is what tasks/mail_watch_renew.py checks so a silent stop becomes an alert
   rather than a quiet inbox.

Everything here goes through agent/store.py, so a concurrent renewal run and
the always-on watcher can't interleave a read-modify-write and drop one of
these fields.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from agent.store import atomic_write_json, load_json, locked

_ROOT = Path(__file__).resolve().parent.parent.parent
_STORE_PATH = _ROOT / "config" / "mail_state.json"

def _defaults() -> dict:
    """A FRESH empty store every call. Deliberately a function, not a module
    constant: load_json returns its `default` object as-is when the file does
    not exist yet, and callers here mutate what they get back. A shared dict
    literal would have `seen` appended to in place, so the module global would
    accumulate every message id the process ever handled — and, in the test
    suite, leak them between tests."""
    return {"history_id": None, "watch_expiration": None, "seen": []}

# How many recently-handled Gmail message ids to remember. Gmail's history is
# only about a week deep, so an id older than the window can never come back
# through list_history() and holding it forever just grows the file. 500 is
# comfortably more than a week of a labelled subset of one mailbox.
MAX_SEEN = 500


def _load() -> dict:
    defaults = _defaults()
    data = load_json(_STORE_PATH, defaults)
    # A store written by an older version (or hand-edited) may be missing keys.
    for key, value in defaults.items():
        data.setdefault(key, value)
    return data


def _save(data: dict) -> None:
    # Prune on write so the store can't grow unbounded (the convention every
    # polling store here follows). Newest ids are at the end.
    data["seen"] = data["seen"][-MAX_SEEN:]
    atomic_write_json(_STORE_PATH, data)


def _newer(stored, incoming) -> str | None:
    """The later of two Gmail historyIds, as a string.

    They are decimal integers that only increase, but Gmail returns them as
    strings and a string compare gets "9" > "10" wrong. Compare as ints, keep
    the string. A value that won't parse is treated as absent rather than
    crashing the watcher's callback."""
    def as_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    a, b = as_int(stored), as_int(incoming)
    if b is None:
        return str(stored) if a is not None else None
    if a is None or b > a:
        return str(incoming)
    return str(stored)


def load_state() -> dict:
    """The whole store. Lock-free: atomic writes mean a reader sees the old or
    the new complete file, never a torn one."""
    return _load()


def history_id() -> str | None:
    return _load()["history_id"]


def unseen(keys) -> list[str]:
    """Those of `keys` not already handled, order preserved.

    Keys, not plain message ids: the watcher handles one message in more than
    one way, and it suffixes the key with what it did (see tasks/mail_watcher.py).
    A message he was told about must still be actionable when he labels it
    afterwards, and that only works if the two are different entries.

    Read-only on purpose. The watcher checks here, does its work, and only then
    calls commit() — marking them seen up front would silently swallow a message
    whose push failed."""
    seen = set(_load()["seen"])
    out, batch = [], set()
    for mid in keys:
        if mid not in seen and mid not in batch:
            batch.add(mid)
            out.append(mid)
    return out


def commit(seen_ids=None, new_history_id=None) -> dict:
    """Record handled messages and advance the watermark, in one locked write.

    The watcher calls this **before** acking the Pub/Sub message. Acking first
    would turn a crash between the two into lost mail: Pub/Sub considers the
    notification delivered while nothing here remembers it was handled.

    The watermark only moves forward (see _newer), so an out-of-order
    notification carrying an older historyId leaves it alone."""
    with locked(_STORE_PATH):
        data = _load()
        for mid in seen_ids or []:
            if mid not in data["seen"]:
                data["seen"].append(mid)
        if new_history_id is not None:
            data["history_id"] = _newer(data["history_id"], new_history_id)
        _save(data)
        return data


def record_watch(expiration, new_history_id=None) -> dict:
    """Store what users.watch() returned: when the watch dies, and the mailbox
    historyId at the moment it was registered.

    The history id is a plain assignment through the same forward-only rule —
    on a first-ever watch it seeds the watermark, and on a renewal it is already
    at or ahead of where we are."""
    with locked(_STORE_PATH):
        data = _load()
        data["watch_expiration"] = expiration
        if new_history_id is not None:
            data["history_id"] = _newer(data["history_id"], new_history_id)
        _save(data)
        return data


def watch_expires_in_hours(now=None) -> float | None:
    """Hours until the stored watch expires, or None if none is stored.

    Gmail reports the expiration as epoch **milliseconds** (a string). Negative
    means it has already lapsed — which is the silent failure this whole store
    exists to make loud."""
    raw = _load()["watch_expiration"]
    try:
        expires_at = datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None
    now = now or datetime.now(timezone.utc)
    return (expires_at - now).total_seconds() / 3600


def main(argv=None) -> int:
    """Print the current state. No arguments — this is a look, not a knob."""
    state = load_state()
    hours = watch_expires_in_hours()
    print(json.dumps({
        "path": str(_STORE_PATH),
        "history_id": state["history_id"],
        "watch_expiration": state["watch_expiration"],
        "watch_expires_in_hours": None if hours is None else round(hours, 1),
        "seen_count": len(state["seen"]),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
