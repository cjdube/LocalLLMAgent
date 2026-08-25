"""Background tasks — Wren runs a multi-step job detached from the chat turn and
pushes the user a summary when it's done.

Execution posture is "A + push-to-approve" (see the Phase 2 plan): the worker
(tasks/bg_worker.py) reads/researches/drafts freely, but any consequential,
irreversible action (send_email, etc. — see toolset.CONSEQUENTIAL_TOOLS) pauses
and is pushed to the user's phone to approve. Untrusted content pulled mid-task
therefore can never trigger an unattended irreversible action.

This module owns the job store and the approval-token logic; the worker owns
execution. State lives in config/bg_jobs.json, written atomically under a
cross-process file lock (agent/store.py) so the Flask chat server, the worker,
and the approval endpoint never read a half-written file or clobber each
other's updates.

A job moves through:
    pending -> (worker) -> done | failed
                        -> awaiting_approval -> approved | denied -> (worker) -> ...

Usage:
    python -m agent.tools.background --list
    python -m agent.tools.background --approve <job_id>   # CLI fallback when the
    python -m agent.tools.background --deny <job_id>      # push buttons expired
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from agent import prefs
from agent.store import atomic_write_json, load_json, locked

_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_ROOT / "config" / ".env")

_STORE_PATH = _ROOT / "config" / "bg_jobs.json"

# The user's name, for the model-facing tool descriptions below. From
# config/preferences.json; falls back to "the user".
_NAME = prefs.user_name()

# Approval tokens are short-lived and single-purpose; reuse the Flask secret so
# there's no extra key to manage. Single-use is enforced by the job state
# machine (resolve_job only acts on an awaiting_approval job), not a token store.
_TOKEN_MAX_AGE_S = 3600
_TOKEN_SALT = "bg-approve"

# Statuses the worker will pick up and act on, oldest first.
_ACTIONABLE = ("pending", "approved", "denied")


RUN_IN_BACKGROUND_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_in_background",
        "description": f"Kick off a multi-step task to run in the background and notify {_NAME} with "
        "a summary when it's done — for things that take a while or that they want to walk away "
        "from. You do NOT do the task now; you hand it off. Any consequential action it needs "
        f"(like sending an email) is routed to {_NAME}'s phone for approval automatically.",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "A complete, self-contained description of what to do — everything "
                    "the background run needs to know, since it can't ask follow-up questions.",
                },
            },
            "required": ["task"],
        },
    },
}

LIST_BG_JOBS_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_background_jobs",
        "description": f"List {_NAME}'s recent background jobs and their status (pending, "
        "awaiting_approval, done, failed). Use when they ask what's running or what happened to a task.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

GET_JOB_RESULT_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_job_result",
        "description": "Get the full result/summary of a finished background job by its id (the push "
        "notification only carries a short version).",
        "parameters": {
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "The id of the job."}},
            "required": ["job_id"],
        },
    },
}


def _load() -> dict:
    return load_json(_STORE_PATH, {"jobs": []})


# Keep the store bounded: it's re-read on every worker poll (every 30s,
# forever) and list_background_jobs feeds the model's context. Terminal jobs
# old enough that their result has long been read fall off on the next write;
# pending/awaiting jobs are never pruned.
_PRUNE_TERMINAL_AFTER_S = 14 * 24 * 3600
_LIST_LIMIT = 20


def _prune(data: dict, now: datetime | None = None) -> None:
    now = now or datetime.now()

    def keep(job: dict) -> bool:
        if job["status"] not in ("done", "failed"):
            return True
        try:
            age = (now - datetime.fromisoformat(job["updated"])).total_seconds()
        except (ValueError, TypeError):
            return True  # unparseable timestamp: keep rather than guess
        return age <= _PRUNE_TERMINAL_AFTER_S

    data["jobs"] = [j for j in data["jobs"] if keep(j)]


def _save(data: dict) -> None:
    _prune(data)
    atomic_write_json(_STORE_PATH, data)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _find(jobs: list, job_id: str) -> dict | None:
    return next((j for j in jobs if j["id"] == job_id), None)


# ---- chat tools -----------------------------------------------------------

def run_in_background(task: str) -> dict:
    """The model-facing tool. Deliberately has no origin parameter: the gates a
    job runs under follow from where it came from, and a job started by the
    model is a chat job by definition. Anything else would let text in the
    model's context choose its own gates."""
    return start_job(task)


def start_job(task: str, origin: str = "chat") -> dict:
    """Queue a background job. `origin` is a provenance flag, not a policy: the
    worker hands it to toolset.confirm_set_for(), which alone decides what a
    "mail" job may do without a tap. Passing a tool list in from the caller
    instead would put that policy in two files, and they drift."""
    task = (task or "").strip()
    if not task:
        return {"error": "task description was empty"}
    job = {
        "id": uuid4().hex[:8],
        "task_text": task,
        "origin": origin,
        "status": "pending",
        "messages": None,
        "pending_call": None,
        "result": None,
        "attempts": 0,
        "created": _now(),
        "updated": _now(),
    }
    with locked(_STORE_PATH):
        data = _load()
        data["jobs"].append(job)
        _save(data)
    return {"id": job["id"], "status": "pending",
            "note": "Started — I'll notify you when it's done."}


def list_background_jobs() -> dict:
    with locked(_STORE_PATH):
        jobs = _load()["jobs"]
    newest_first = sorted(jobs, key=lambda j: j["created"], reverse=True)
    return {
        "count": len(jobs),
        # Capped: this listing lands in the model's context window.
        "jobs": [
            {"id": j["id"], "status": j["status"], "task": j["task_text"][:120],
             "created": j["created"]}
            for j in newest_first[:_LIST_LIMIT]
        ],
    }


def get_job_result(job_id: str) -> dict:
    with locked(_STORE_PATH):
        job = _find(_load()["jobs"], job_id)
    if job is None:
        return {"error": f"no background job with id {job_id!r}"}
    return {"id": job["id"], "status": job["status"], "task": job["task_text"],
            "result": job["result"]}


# ---- worker-facing state machine -----------------------------------------

def next_actionable() -> dict | None:
    """The oldest job the worker should act on (pending/approved/denied), or None."""
    with locked(_STORE_PATH):
        jobs = _load()["jobs"]
    actionable = [j for j in jobs if j["status"] in _ACTIONABLE]
    return min(actionable, key=lambda j: j["created"]) if actionable else None


def _update(job_id: str, **fields) -> None:
    with locked(_STORE_PATH):
        data = _load()
        job = _find(data["jobs"], job_id)
        if job is None:
            return
        job.update(fields, updated=_now())
        _save(data)


def save_awaiting(job_id: str, messages: list, pending_call: dict,
                  approval_message: str | None = None) -> None:
    """Persist a paused run: its full conversation + the call awaiting approval.
    approval_message is the human text of the approval push, stored so a
    re-push for a stale job (see stale_awaiting) doesn't need the heavy
    describer stack the worker only loads when actually running a job."""
    _update(job_id, status="awaiting_approval", messages=messages,
            pending_call=pending_call, approval_message=approval_message)


def mark_resumed(job_id: str, messages: list) -> None:
    """Persist the conversation right after a resolved approval/denial and hand
    the job back to the pending queue. Persisting at this boundary makes a
    transient-failure retry resume from AFTER the resolved call — so an
    approved consequential action can't execute a second time."""
    _update(job_id, status="pending", messages=messages, pending_call=None)


def mark_done(job_id: str, result: str) -> None:
    _update(job_id, status="done", result=result, messages=None, pending_call=None)


def mark_failed(job_id: str, error: str) -> None:
    _update(job_id, status="failed", result=f"failed: {error}", messages=None, pending_call=None)


def bump_attempts(job_id: str) -> int:
    """Increment and return the job's transient-failure attempt count (the
    worker retries a transient error a bounded number of times)."""
    with locked(_STORE_PATH):
        data = _load()
        job = _find(data["jobs"], job_id)
        if job is None:
            return 0
        job["attempts"] = job.get("attempts", 0) + 1
        job["updated"] = _now()
        _save(data)
        return job["attempts"]


def stale_awaiting(max_age_s: int, now: datetime | None = None) -> list:
    """Jobs stuck in awaiting_approval whose last update is older than
    max_age_s — i.e. their approval push's tokens have expired (or it never had
    buttons because WREN_PUBLIC_URL was unset). The worker re-pushes these;
    touch() resets the clock so each re-push happens once per interval."""
    now = now or datetime.now()
    out = []
    with locked(_STORE_PATH):
        jobs = _load()["jobs"]
    for j in jobs:
        if j["status"] != "awaiting_approval":
            continue
        try:
            age = (now - datetime.fromisoformat(j["updated"])).total_seconds()
        except (ValueError, TypeError):
            age = max_age_s + 1  # unparseable timestamp: treat as stale
        if age > max_age_s:
            out.append(j)
    return out


def touch(job_id: str) -> None:
    """Refresh the job's updated timestamp (e.g. after re-pushing its approval)."""
    _update(job_id)


def resolve_job(job_id: str, approved: bool) -> bool:
    """Approve/deny a job awaiting approval. Returns True if applied, False if the
    job isn't awaiting (unknown, or already resolved — makes the token single-use)."""
    with locked(_STORE_PATH):
        data = _load()
        job = _find(data["jobs"], job_id)
        if job is None or job["status"] != "awaiting_approval":
            return False
        job["status"] = "approved" if approved else "denied"
        job["updated"] = _now()
        _save(data)
    return True


# ---- approval tokens & phone-push actions ---------------------------------

def _serializer() -> URLSafeTimedSerializer:
    secret = os.getenv("FLASK_SECRET_KEY")
    if not secret:
        raise RuntimeError("FLASK_SECRET_KEY must be set to sign background-approval tokens")
    return URLSafeTimedSerializer(secret, salt=_TOKEN_SALT)


def make_approval_token(job_id: str, decision: str) -> str:
    return _serializer().dumps({"job": job_id, "decision": decision})


def read_approval_token(token: str) -> dict | None:
    """Verify a signed approval token; return its payload or None if bad/expired."""
    try:
        return _serializer().loads(token, max_age=_TOKEN_MAX_AGE_S)
    except (BadSignature, SignatureExpired):
        return None


def approval_actions(job_id: str) -> list | None:
    """ntfy action buttons for the approval push, or None if WREN_PUBLIC_URL isn't
    set (in which case the push still goes, just without tap-to-approve buttons)."""
    base = os.getenv("WREN_PUBLIC_URL", "").rstrip("/")
    if not base:
        return None
    endpoint = f"{base}/api/bg/resolve"
    return [
        {"action": "http", "label": "Approve",
         "url": f"{endpoint}?token={make_approval_token(job_id, 'approve')}",
         "method": "POST", "clear": True},
        {"action": "http", "label": "Deny",
         "url": f"{endpoint}?token={make_approval_token(job_id, 'deny')}",
         "method": "POST", "clear": True},
    ]


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--approve", metavar="JOB_ID",
                        help="approve a job stuck awaiting_approval (fallback "
                        "when the push's buttons expired or never rendered)")
    parser.add_argument("--deny", metavar="JOB_ID", help="deny a job awaiting approval")
    args = parser.parse_args(argv)
    if args.approve or args.deny:
        job_id = args.approve or args.deny
        applied = resolve_job(job_id, approved=bool(args.approve))
        print(json.dumps({"ok": applied, "job": job_id,
                          "decision": "approve" if args.approve else "deny"}))
        if not applied:
            print(f"no job {job_id!r} awaiting approval", file=sys.stderr)
        return 0 if applied else 1
    if args.list:
        print(json.dumps(list_background_jobs(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
