"""Read back the phone pushes Wren has actually sent.

Every push in the system funnels through one function — notify() in
agent/tools/notify.py — and until this module none of them were written down.
Asked "what were the last 3 notifications you sent me?", Wren could only say she
had no history, which was true: reminders are the clearest case, since
reminders.complete() deletes a reminder the moment its push lands ("the push
itself is the record"). Everything else — task-failure alerts, the morning
synthesis nudges, the log-inspector rollup, background-job approval prompts —
was equally gone once it scrolled off the phone.

So notify() calls record() here on a successful send, and list_notifications()
reads it back for chat. Both halves live in one module so the writer and the
reader can't drift onto different shapes (same reason nudges.py owns
SYNTHESIS_DIR for daily_synthesis).

Only DELIVERED pushes are recorded. tasks/reminder_sweep.py re-pushes a failed
reminder every 60 seconds until it lands, so logging attempts would have written
tens of thousands of rows during the four-day ntfy outage of July 2026.

State lives in config/push_log.json, written atomically under a cross-process
file lock (agent/store.py): the Flask chat server, the reminder sweeper and the
background worker all push, from separate processes.

Usage:
    python -m agent.tools.push_log --days 7
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from agent import prefs
from agent.dates import local_timezone
from agent.store import atomic_write_json, load_json, locked
from agent.tools._http import print_result

_ROOT = Path(__file__).resolve().parent.parent.parent
_STORE_PATH = _ROOT / "config" / "push_log.json"

# Whose phone these went to, for the model-facing description below. From
# config/preferences.json; falls back to "the user".
_NAME = prefs.user_name()

# The window the tool looks back over when the caller doesn't say. A week covers
# "what did you send me?" and "did you ping me yesterday?" without pulling a
# month of failure alerts into the small model's context.
DEFAULT_DAYS = 7

# Bounds on the requested window. The floor stops a 0 or negative from silently
# meaning "nothing"; the ceiling matches the store's own retention, so a larger
# number can't promise rows that were already pruned.
MIN_DAYS = 1
MAX_DAYS = 30

# How many rows one answer may carry. The store holds far more than a chat turn
# should read; a busy log_inspector night alone can push several alerts.
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

# Char budget for the payload. A row cap doesn't bound size: MAX_LIMIT counts
# rows, and each message runs to notify._MAX_MESSAGE_CHARS. Over budget the
# answer carries fewer rows and _render's header says how many of how many.
MAX_PAYLOAD_CHARS = 12000

# Retention. reminder_sweep touches this store on every fire, forever, so it
# prunes on write like agent/tools/background.py does: by age first, then a hard
# cap so a burst can't outrun the age window.
_PRUNE_AFTER_DAYS = 30
_MAX_ROWS = 500


def _now() -> datetime:
    """Now in the local zone. The rows are compared against local day windows
    ("did you send me anything yesterday?"), so they're stamped in the same zone
    they're read in — a UTC stamp would shift every evening's rows by a day."""
    return datetime.now(ZoneInfo(local_timezone()))


def _load() -> dict:
    return load_json(_STORE_PATH, {"pushes": []})


def _prune(data: dict, now: datetime | None = None) -> None:
    now = now or _now()
    cutoff = now - timedelta(days=_PRUNE_AFTER_DAYS)

    def keep(row: dict) -> bool:
        try:
            return datetime.fromisoformat(row["ts"]) >= cutoff
        except (KeyError, ValueError, TypeError):
            return False  # unstamped row: can't be windowed or reported, drop it

    data["pushes"] = [r for r in data["pushes"] if keep(r)][-_MAX_ROWS:]


def _save(data: dict) -> None:
    _prune(data)
    atomic_write_json(_STORE_PATH, data)


def record(message: str, title: str | None = None, priority: str | None = None) -> None:
    """Append one delivered push. Called by notify() on the success path only.

    Takes the body notify() actually sent (already truncated), not the caller's
    original message, so the log says what landed on the phone."""
    row = {
        "ts": _now().isoformat(timespec="seconds"),
        "title": title,
        "message": message,
        "priority": priority,
    }
    with locked(_STORE_PATH):
        data = _load()
        data["pushes"].append(row)
        _save(data)


def _human_ts(ts: str) -> str:
    """The stamp as a person reads it. Python owns this formatting — the model is
    never asked to turn an ISO timestamp into a date."""
    try:
        return datetime.fromisoformat(ts).strftime("%a %b %-d, %-I:%M %p")
    except (ValueError, TypeError):
        return ts


def _render(rows: list, days: int, total: int = None) -> str:
    """The rows as a finished answer the model relays instead of composing.

    Same reasoning as nudges._render: asked to write a list out itself, the small
    model paraphrases, and a paraphrased notification is indistinguishable from
    an invented one. Here that risk is higher, not lower — a push is a single
    line of plain text, exactly the shape pretraining can fake."""
    if not rows:
        return (f"Nothing was pushed in the last {days} days. That is normal — "
                "many days produce no notification at all.")
    lines = []
    for row in rows:
        title = f"**{row['title']}** — " if row["title"] else ""
        lines.append(f"- {row['when']} — {title}{row['message']}")
    # `total` is the count BEFORE the limit slice. Counting len(rows) here was a
    # flat falsehood, not an omission: the header said "20 notification(s) from
    # the last 30 days" when 41 were sent, and this block is what the model is
    # told to relay verbatim — so it repeated the wrong number word for word.
    if total is None or total == len(rows):
        header = f"{len(rows)} notification(s) from the last {days} days, newest first:"
    else:
        header = (f"The {len(rows)} most recent of {total} notification(s) from the "
                  f"last {days} days — ask for a higher limit to see the rest:")
    return header + "\n" + "\n".join(lines)


def _clamp(value, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def list_notifications(days: int = DEFAULT_DAYS, limit: int = DEFAULT_LIMIT) -> dict:
    """The pushes delivered in the last `days` days as `summary` — the rows
    already formatted (see _render) — plus the counts behind it.

    The rows themselves are deliberately NOT returned. TOOL_SCHEMA tells the
    model to reply with `summary` verbatim and never to compose the list itself,
    so a second copy has no reader: it doubled the payload (4463 chars against
    summary's 3513 on a normal week, which is what pushed an ordinary call past
    the agent loop's cap), and the only field it added over `summary` was the raw
    ISO `ts` — the one form the model must never do date math on, when `summary`
    already carries the same stamp written out for a human.

    `days` is clamped to MIN_DAYS..MAX_DAYS and `limit` to 1..MAX_LIMIT. An
    empty or absent store is "nothing sent", not an error: push is allowed to be
    switched off entirely (NTFY_URL unset)."""
    days = _clamp(days, DEFAULT_DAYS, MIN_DAYS, MAX_DAYS)
    limit = _clamp(limit, DEFAULT_LIMIT, 1, MAX_LIMIT)

    cutoff = _now() - timedelta(days=days)
    rows = []
    for row in _load()["pushes"]:
        try:
            when = datetime.fromisoformat(row["ts"])
        except (KeyError, ValueError, TypeError):
            continue
        if when < cutoff:
            continue
        rows.append({
            "ts": row["ts"],
            "when": _human_ts(row["ts"]),
            "title": row.get("title"),
            "message": row.get("message", ""),
        })

    # Newest first: the last thing she sent is what's usually being asked about.
    rows.sort(key=lambda r: r["ts"], reverse=True)
    total = len(rows)
    rows = rows[:limit]
    return _fit(rows, days, total)


def _fit(rows: list, days: int, total: int) -> dict:
    """The payload, carrying fewer rows until it fits MAX_PAYLOAD_CHARS.

    `total` stays the count before any slice, so _render's own "N of M" header
    states the real number — a reduced answer reads as reduced instead of
    reading as everything there was."""
    def build(rs: list) -> dict:
        return {"summary": _render(rs, days, total), "total": total,
                "shown": len(rs), "days": days}

    out = build(rows)
    # Oldest first: the last thing she sent is what's usually being asked about,
    # so a shorter answer keeps the newest rows.
    while len(json.dumps(out)) > MAX_PAYLOAD_CHARS and len(rows) > 1:
        rows = rows[:-1]
        out = build(rows)
    return out


TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_notifications",
        # Registry-style "what exists?" tool, so the wording carries the same
        # weight it does in nudges.py and games.py: asked what she sent, the
        # model has a plausible answer available from the conversation and from
        # pretraining, and a fabricated notification — one line of plain text —
        # is shaped exactly like a real one. Hence the flat statement that the
        # list is not something it knows.
        "description": (
            f"List the notifications Wren has actually pushed to {_NAME}'s phone — "
            "fired reminders, scheduled-task failure alerts, morning synthesis "
            f"nudges and approval requests. Call this whenever {_NAME} asks what you "
            "sent, pushed, notified, alerted, pinged or texted him about, what "
            "notifications or ntfy messages he got, or refers back to one. This is "
            "the ONLY way to see a notification that already fired: list_reminders "
            "shows only reminders still pending, never ones already sent. This list "
            "is NOT something you know: only the notifications this tool returns "
            "were ever sent. Never invent, paraphrase or reconstruct one you did not "
            "get back from the tool. Reply with the tool's `summary` field exactly as "
            "it comes back — it is the finished answer, already listing every "
            "notification with its time. Do not retype the list yourself, do not "
            "shorten it, and do not describe what it was about; the notifications ARE "
            "the answer. `summary` also carries the wording for an empty window."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": (
                        f"How many days back to look. Defaults to {DEFAULT_DAYS}; "
                        f"capped at {MAX_DAYS}."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"How many notifications to return at most, newest first. "
                        f"Defaults to {DEFAULT_LIMIT}. Set it when {_NAME} asks for a "
                        "specific number ('the last 3')."
                    ),
                },
            },
        },
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()
    return print_result(list_notifications(days=args.days, limit=args.limit))


if __name__ == "__main__":
    sys.exit(main())
