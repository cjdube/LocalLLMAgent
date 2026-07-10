"""Background tasks — Wren runs a multi-step job detached from the chat turn and
pushes Craig a summary when it's done.

Execution posture is "A + push-to-approve" (see the Phase 2 plan): the worker
(tasks/bg_worker.py) reads/researches/drafts freely, but any consequential,
irreversible action (send_email, etc. — see toolset.CONSEQUENTIAL_TOOLS) pauses
and is pushed to Craig's phone to approve. Untrusted content pulled mid-task
therefore can never trigger an unattended irreversible action.

This module owns the job store and the approval-token logic; the worker owns
execution. State lives in config/bg_jobs.json, written atomically under a lock
(same model as agent/tools/reminders.py) so the Flask chat server, the worker,
and the approval endpoint never read a half-written file.

A job moves through:
    pending -> (worker) -> done | failed
                        -> awaiting_approval -> approved | denied -> (worker) -> ...

Usage:
    python -m agent.tools.background --list
"""

import argparse
import json
import os
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_ROOT / "config" / ".env")

_STORE_PATH = _ROOT / "config" / "bg_jobs.json"
_LOCK = threading.Lock()

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
        "description": "Kick off a multi-step task to run in the background and notify Craig with a "
        "summary when it's done. Use this when Craig asks you to go do something that will take a "
        "while or that he wants to walk away from (research, gather and compile, watch for and "
        "report). You do NOT do the task now — you hand it off, and it runs detached. Any "
        "consequential action it needs (like sending an email) is automatically routed to Craig's "
        "phone for approval, so describe the task fully and let it proceed.",
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
        "description": "List Craig's recent background jobs and their status (pending, "
        "awaiting_approval, done, failed). Use when he asks what's running or what happened to a task.",
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
    try:
        return json.loads(_STORE_PATH.read_text())
    except FileNotFoundError:
        return {"jobs": []}


def _save(data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=_STORE_PATH.parent, prefix=".bg_jobs.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _STORE_PATH)
    except BaseException:
        os.unlink(tmp)
        raise


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _find(jobs: list, job_id: str) -> dict | None:
    return next((j for j in jobs if j["id"] == job_id), None)


# ---- chat tools -----------------------------------------------------------

def run_in_background(task: str) -> dict:
    task = (task or "").strip()
    if not task:
        return {"error": "task description was empty"}
    job = {
        "id": uuid4().hex[:8],
        "task_text": task,
        "status": "pending",
        "messages": None,
        "pending_call": None,
        "result": None,
        "created": _now(),
        "updated": _now(),
    }
    with _LOCK:
        data = _load()
        data["jobs"].append(job)
        _save(data)
    return {"id": job["id"], "status": "pending",
            "note": "Started — I'll notify you when it's done."}


def list_background_jobs() -> dict:
    with _LOCK:
        jobs = _load()["jobs"]
    return {
        "count": len(jobs),
        "jobs": [
            {"id": j["id"], "status": j["status"], "task": j["task_text"][:120],
             "created": j["created"]}
            for j in sorted(jobs, key=lambda j: j["created"], reverse=True)
        ],
    }


def get_job_result(job_id: str) -> dict:
    with _LOCK:
        job = _find(_load()["jobs"], job_id)
    if job is None:
        return {"error": f"no background job with id {job_id!r}"}
    return {"id": job["id"], "status": job["status"], "task": job["task_text"],
            "result": job["result"]}


# ---- worker-facing state machine -----------------------------------------

def next_actionable() -> dict | None:
    """The oldest job the worker should act on (pending/approved/denied), or None."""
    with _LOCK:
        jobs = _load()["jobs"]
    actionable = [j for j in jobs if j["status"] in _ACTIONABLE]
    return min(actionable, key=lambda j: j["created"]) if actionable else None


def _update(job_id: str, **fields) -> None:
    with _LOCK:
        data = _load()
        job = _find(data["jobs"], job_id)
        if job is None:
            return
        job.update(fields, updated=_now())
        _save(data)


def save_awaiting(job_id: str, messages: list, pending_call: dict) -> None:
    """Persist a paused run: its full conversation + the call awaiting approval."""
    _update(job_id, status="awaiting_approval", messages=messages, pending_call=pending_call)


def mark_done(job_id: str, result: str) -> None:
    _update(job_id, status="done", result=result, messages=None, pending_call=None)


def mark_failed(job_id: str, error: str) -> None:
    _update(job_id, status="failed", result=f"failed: {error}", messages=None, pending_call=None)


def resolve_job(job_id: str, approved: bool) -> bool:
    """Approve/deny a job awaiting approval. Returns True if applied, False if the
    job isn't awaiting (unknown, or already resolved — makes the token single-use)."""
    with _LOCK:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        print(json.dumps(list_background_jobs(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
