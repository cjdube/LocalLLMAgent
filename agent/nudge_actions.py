"""The one-tap actions on a daily-synthesis nudge.

tasks/daily_synthesis.py pushes up to three "these line up" nudges a morning,
and almost every one ends in a question — "update the wiki?", "add it to the
idea?". Until this module the only way to say yes was to open chat and type it.
Now each nudge carries one ntfy button, and the tap does the action here, in
Python, with no model call:

- **wiki** — write a source note to the vault's `raw/` (LEARNINGS_DIR) holding
  the *facts* behind the nudge: what the user did, word for word, the page
  address, and the page's own text when it can be fetched. ObsidianWikiAgent
  ingests it on its next run. The nudge's own question is deliberately NOT in
  the note: a question filed as a source is what produced the fabricated page
  recorded in docs/daily-synthesis.md ("Why the archive is not in raw/").
- **ticket** — the same facts as a comment on the parked ClickUp item.

Python picks the action from the candidate's anchor kind when the nudge is
pushed (action_for); the model only writes the line. The tap is the approval —
the user read the nudge and chose it — so there is no second confirmation.

Each pushed nudge is a row in config/nudge_actions.json, kept RETENTION_DAYS. A
button's token names one row and is signed with the Flask secret, like the
background approvals, but lives as long as the row: a 5:45 AM push read at
lunch must still work. Single-use falls out of the row's `done` stamp. The
`done` stamps are also the record of which nudges were acted on.
"""

import secrets
from datetime import datetime, timedelta
from pathlib import Path

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from agent import config, prefs
from agent.store import atomic_write_json, load_json, locked
from agent.tools.clickup import comment_on_clickup_task
from agent.tools.learnings_file import write_entry
from agent.tools.web_fetch import fetch_webpage

_ROOT = Path(__file__).resolve().parent.parent
_STORE_PATH = _ROOT / "config" / "nudge_actions.json"

RETENTION_DAYS = 7
_TOKEN_MAX_AGE_S = RETENTION_DAYS * 86400
_TOKEN_SALT = "nudge-act"

# Page text for a browsed page is a source excerpt, not the whole site. Bounds
# what one tap puts in front of the ingest model.
MAX_PAGE_CHARS = 6000

# Anchor kind (as tasks/daily_synthesis.py labels it) -> action. A company has
# no button: there is no note or ticket to attach a company to yet.
_ANCHOR_ACTIONS = {
    "wiki page": "wiki",
    "project you're building": "wiki",
    "idea you parked": "ticket",
}

# The button's label and the words the model is told the button does, so the
# question it writes matches what the tap will do.
LABELS = {"wiki": "wiki", "ticket": "ticket"}
DESCRIPTIONS = {
    "wiki": "save the facts as a note for his wiki",
    "ticket": "add the facts as a comment on that ClickUp item",
}


def action_for(pair: dict) -> str | None:
    """The one action a candidate pair offers, decided in Python."""
    if pair["kind"] == "cross":
        return "wiki"  # an echo is "worth a note?" by definition
    return _ANCHOR_ACTIONS.get(pair["anchor"]["kind"])


def _load() -> dict:
    return load_json(_STORE_PATH, {"nudges": []})


def _prune(data: dict) -> None:
    cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).isoformat()
    data["nudges"] = [n for n in data["nudges"] if n.get("created", "") >= cutoff]


def _evidence(pair: dict) -> list:
    """What the user did, as {kind, text, urls} — the facts a note carries."""
    signals = [pair["signal"]] + ([pair["other"]] if pair["kind"] == "cross" else [])
    return [{"kind": s["kind"], "text": s["text"], "urls": s.get("urls", [])}
            for s in signals]


def record(day, tagged: list) -> list:
    """Store each (text, pair) nudge that has an action; return one row per
    nudge in the same order — None where the nudge gets no button."""
    rows = []
    with locked(_STORE_PATH):
        data = _load()
        _prune(data)
        for text, pair in tagged:
            action = action_for(pair) if pair else None
            if not action:
                rows.append(None)
                continue
            row = {
                "id": secrets.token_urlsafe(8),
                "day": f"{day:%Y-%m-%d}",
                "text": text,
                "action": action,
                "target": pair["anchor"]["label"] if pair["kind"] == "anchor" else None,
                "target_kind": pair["anchor"]["kind"] if pair["kind"] == "anchor" else None,
                "evidence": _evidence(pair),
                "created": datetime.now().isoformat(timespec="seconds"),
                "done": None,
                "result": None,
            }
            data["nudges"].append(row)
            rows.append(row)
        atomic_write_json(_STORE_PATH, data)
    return rows


def _serializer() -> URLSafeTimedSerializer:
    secret = config.getenv("FLASK_SECRET_KEY")
    if not secret:
        raise RuntimeError("FLASK_SECRET_KEY must be set to sign nudge-action tokens")
    return URLSafeTimedSerializer(secret, salt=_TOKEN_SALT)


def make_token(nudge_id: str) -> str:
    return _serializer().dumps({"nudge": nudge_id})


def read_token(token: str) -> str | None:
    """The nudge id a token names, or None if it is bad or expired."""
    try:
        return _serializer().loads(token, max_age=_TOKEN_MAX_AGE_S).get("nudge")
    except (BadSignature, SignatureExpired):
        return None


def buttons(rows: list) -> list | None:
    """ntfy action buttons, one per nudge that has an action, labelled with the
    nudge's number in the push. None when WREN_PUBLIC_URL is unset (the push
    still goes, without buttons) or nothing has an action."""
    base = config.getenv("WREN_PUBLIC_URL", "").rstrip("/")
    if not base:
        return None
    actions = [
        {"action": "http", "label": f"{i} → {LABELS[row['action']]}",
         "url": f"{base}/api/nudges/act?token={make_token(row['id'])}",
         "method": "POST", "clear": False}
        for i, row in enumerate(rows, 1) if row
    ]
    return actions or None


def _facts(row: dict) -> list:
    return [f"- {e['kind']}: {e['text']}" for e in row["evidence"]]


def _note(row: dict) -> str:
    """The source note. Facts only — never the nudge's question."""
    name = prefs.user_name()
    if row["target_kind"] == "wiki page":
        relates = f"Relates to: [[{row['target']}]]"
    elif row["target"]:
        relates = f"Relates to his {row['target_kind']}: {row['target']}"
    else:
        relates = "The same theme reached him through two channels that day."
    lines = [f"# Activity on {row['day']}", "",
             f"Saved by {name} from Wren's daily synthesis.", relates, "",
             f"## What {name} did on {row['day']}", *_facts(row)]
    for url in [u for e in row["evidence"] for u in e["urls"]][:1]:
        page = fetch_webpage(url=url, max_chars=MAX_PAGE_CHARS)
        if page.get("markdown"):
            lines += ["", f"## Page text ({url})", page["markdown"]]
    return "\n".join(lines) + "\n"


def _do(row: dict) -> dict:
    if row["action"] == "wiki":
        day = datetime.fromisoformat(row["day"])
        return write_entry(_note(row), f"Wren-Nudge-{row['id']}", day)
    comment = (f"Related activity on {row['day']} (from Wren's daily synthesis):\n"
               + "\n".join(_facts(row)))
    return comment_on_clickup_task(row["target"], comment)


def perform(nudge_id: str) -> dict:
    """Run a nudge's action once. Returns {"done": True, "row": ...}, {"already":
    True} for a repeat tap, or {"error": ...}. A failed action leaves the row
    open, so a second tap can retry it."""
    with locked(_STORE_PATH):
        data = _load()
        row = next((n for n in data["nudges"] if n["id"] == nudge_id), None)
        if row is None:
            return {"error": "that nudge has expired"}
        if row["done"]:
            return {"already": True, "row": row}
        # Under the lock on purpose: a double tap must not write two notes.
        result = _do(row)
        if "error" in result:
            return {"error": result["error"], "row": row}
        row["done"] = datetime.now().isoformat(timespec="seconds")
        row["result"] = result
        atomic_write_json(_STORE_PATH, data)
    return {"done": True, "row": row}
