"""Escalation log — the record of every manual "redo with the frontier model"
in chat. Two jobs in one store: a privacy audit trail (which conversation went
off-device, when, to which provider) and the instrument that would later justify
or design an automatic router — each record pairs the request with the local
model's rejected reply. See docs/frontier-escalation.md.

Manual-only and low-volume (one deliberate tap each), but pruned on write like
every polling store so a long-lived install can't accrete without bound. Written
through agent.store so the lock/atomic-write guarantees match the other stores.
"""

from datetime import datetime, timezone
from pathlib import Path

from agent.store import atomic_write_json, load_json, locked

_ROOT = Path(__file__).resolve().parent.parent
_STORE_PATH = _ROOT / "config" / "escalations.json"

# Keep the most recent N records. Generous for a hand-driven log, but bounded.
MAX_RECORDS = 200


def record_escalation(
    *,
    request: str,
    local_reply: str,
    prompt_tokens: int,
    backend: str,
    model: str,
    outcome: str,
) -> dict:
    """Append one escalation record (newest last), pruning to MAX_RECORDS, and
    return it. `outcome` is 'ok' or 'error:<reason>'; `prompt_tokens` is the
    approximate size shipped off-device."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "request": request,
        "local_reply": local_reply,
        "prompt_tokens": prompt_tokens,
        "backend": backend,
        "model": model,
        "outcome": outcome,
    }
    with locked(_STORE_PATH):
        data = load_json(_STORE_PATH, {"escalations": []})
        data["escalations"].append(record)
        data["escalations"] = data["escalations"][-MAX_RECORDS:]
        atomic_write_json(_STORE_PATH, data)
    return record
