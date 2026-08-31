"""The queue between the ClickUp tag and the Claude Code run.

Two processes touch it: tasks/clickup_watcher.py puts a job in when a
`wren-build` tag passes its preconditions, and tasks/build_worker.py takes one
out. They are separate launchd jobs because a build runs for minutes to tens of
minutes and the watcher must keep polling every five (see the module docstring
in tasks/build_worker.py for the full argument).

Deliberately not agent/tools/background.py. That store carries a paused
conversation, an approval token and a retry counter, none of which exist here: a
build is approved by the tag, runs once, and either finishes or does not. A job
here is only ever pending -> running -> done | failed.

Kept apart from the worker so the watcher can import the enqueue side without
pulling in subprocess, git or a Claude Code binary.

Usage:
    python -m tasks.build_queue --list
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.store import atomic_write_json, load_json, locked

_STORE_PATH = Path(__file__).resolve().parent.parent / "config" / "build_jobs.json"

# A build job carries the whole plan, so rows here are large — thousands of
# characters each, against a few hundred in bg_jobs.json. Keep far fewer.
_MAX_STORED_JOBS = 40
_MAX_PENDING_JOBS = 5
_TERMINAL = ("done", "failed")

# A job left "running" this long had its worker killed mid-build (a reboot, a
# `launchctl bootout`). Nothing retries it — the worktree may be half-written and
# re-running Claude over it would compound the mess — but it must not sit there
# looking live forever, because "running" is what the /clickup comment told the
# user to expect an answer from.
STALE_RUNNING_S = 6 * 3600


def _load() -> dict:
    return load_json(_STORE_PATH, {"jobs": []})


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _prune(data: dict) -> None:
    """Keep every live job, and only the newest terminal ones."""
    live = [j for j in data["jobs"] if j["status"] not in _TERMINAL]
    terminal = [j for j in data["jobs"] if j["status"] in _TERMINAL]
    terminal.sort(key=lambda j: j.get("updated", ""), reverse=True)
    data["jobs"] = live + terminal[: max(0, _MAX_STORED_JOBS - len(live))]


def enqueue(task_id: str, title: str, plan_text: str, plan_name: str) -> dict:
    """Queue one build. Returns the job, or {"error": ...}.

    The plan text is copied in rather than referenced by URL: the job record is
    then a complete, testable description of what will be built, and a plan
    detached from the ClickUp Task after tagging cannot change what runs.
    """
    if not (plan_text or "").strip():
        return {"error": "the plan was empty"}
    job = {
        "id": uuid4().hex[:8],
        "task_id": task_id,
        "title": title,
        "plan_name": plan_name,
        "plan_text": plan_text,
        "status": "pending",
        "branch": None,
        "worktree": None,
        "report": None,
        "created": _now(),
        "updated": _now(),
    }
    with locked(_STORE_PATH):
        data = _load()
        _prune(data)
        pending = sum(j["status"] == "pending" for j in data["jobs"])
        if pending >= _MAX_PENDING_JOBS:
            # Not a soft cap. Each of these is a paid Claude Code run, and a
            # queue this deep means the worker is not draining — building five
            # more on top of it makes that worse, not better.
            return {"error": f"build queue is full ({_MAX_PENDING_JOBS} waiting)"}
        data["jobs"].append(job)
        _prune(data)
        atomic_write_json(_STORE_PATH, data)
    return job


def next_pending() -> dict | None:
    """The oldest pending job, or None. Read-only: the worker claims it with
    mark_running, which is the call that excludes a second worker."""
    with locked(_STORE_PATH):
        jobs = _load()["jobs"]
    pending = [j for j in jobs if j["status"] == "pending"]
    return min(pending, key=lambda j: j["created"]) if pending else None


def _update(job_id: str, **fields) -> dict | None:
    with locked(_STORE_PATH):
        data = _load()
        job = next((j for j in data["jobs"] if j["id"] == job_id), None)
        if job is None:
            return None
        job.update(fields, updated=_now())
        _prune(data)
        atomic_write_json(_STORE_PATH, data)
        return dict(job)


def mark_running(job_id: str, branch: str, worktree: str) -> bool:
    """Claim a pending job. False if it is not pending any more — which is how a
    second worker started by an overlapping poll finds out to leave it alone."""
    with locked(_STORE_PATH):
        data = _load()
        job = next((j for j in data["jobs"] if j["id"] == job_id), None)
        if job is None or job["status"] != "pending":
            return False
        job.update(status="running", branch=branch, worktree=worktree, updated=_now())
        atomic_write_json(_STORE_PATH, data)
    return True


def mark_done(job_id: str, report: str) -> None:
    _update(job_id, status="done", report=report)


def mark_failed(job_id: str, why: str) -> None:
    _update(job_id, status="failed", report=f"failed: {why}")


def stale_running(max_age_s: int = STALE_RUNNING_S, now: datetime = None) -> list:
    """Jobs stuck in `running` past the point where a worker could still be
    alive — their process was killed rather than their build finishing. The
    worker fails these on its idle pass so they stop reading as live."""
    now = now or datetime.now()
    out = []
    with locked(_STORE_PATH):
        jobs = _load()["jobs"]
    for j in jobs:
        if j["status"] != "running":
            continue
        try:
            age = (now - datetime.fromisoformat(j["updated"])).total_seconds()
        except (ValueError, TypeError):
            age = max_age_s + 1  # unparseable timestamp: treat as stale
        if age > max_age_s:
            out.append(j)
    return out


def list_jobs() -> list:
    """Every job, newest first, without the plan text — which is the bulk of a
    row and is never what you want when you are asking what ran."""
    with locked(_STORE_PATH):
        jobs = _load()["jobs"]
    return [
        {k: v for k, v in j.items() if k != "plan_text"}
        for j in sorted(jobs, key=lambda j: j["created"], reverse=True)
    ]


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true",
                        help="the only mode; accepted so the command reads as it looks")
    parser.parse_args(argv)
    print(json.dumps(list_jobs(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
