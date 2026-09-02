"""Serialize launchd catch-up work after a reboot or interpreter repair.

``launchd/reload-after-upgrade.sh`` can repair a whole set of jobs at once.
It deliberately does *not* start their missed calendar runs: this module keeps
that work in a locked JSON queue and starts one launchd job per poll, after the
local model is reachable when the selected job needs it.

Usage:
    python -m tasks.startup_recovery --enqueue local.wren.morningbrief
    python -m tasks.startup_recovery
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

from agent.loop import resolve_backend
from agent.store import atomic_write_json, load_json, locked
from tasks._common import notify_failure, setup_logger

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / "config" / ".env")
STORE_PATH = _ROOT / "config" / "startup_recovery.json"
DOMAIN = f"gui/{os.getuid()}"
MAX_ATTEMPTS = 3
RETRY_DELAYS_S = (60, 300, 900)

# Every calendar job is deliberately listed once. This is startup policy, not
# task discovery: the queue needs an explicit answer to priority and whether an
# unavailable Ollama should hold the job. The partition check below makes adding
# a scheduled job without making that decision fail loudly.
#
# Wren's own calendar jobs only. ScribeJay's eight left with its repo, and a
# recovery run here could not start them anyway: this queue boots labels through
# `launchctl kickstart`, but it decides WHICH to queue from the plists in this
# checkout, and reads each result from this checkout's logs/. Recovering them is
# ScribeJay's job to own, in its own repo, against its own logs.
POLICIES = {
    "local.wren.morningbrief": ("morning_brief", 0, "wren"),
    "local.wren.mailwatchrenew": ("mail_watch_renew", 0, "none"),
    "local.wren.loginspector": ("log_inspector", 0, "none"),
    "local.wren.opportunitydigest": ("opportunity_digest", 0, "wren"),
    "local.wren.dailysynthesis": ("daily_synthesis", 1, "wren"),
    "local.wren.projectscan": ("project_scan", 1, "wren"),
    "local.wren.starredblurbs": ("starred_blurbs", 1, "wren"),
    "local.wren.starredinstalled": ("starred_installed", 1, "none"),
    "local.wren.starredreleases": ("starred_releases", 1, "none"),
}


def _now() -> datetime:
    return datetime.now()


def _default() -> dict:
    return {"queue": [], "active": None, "results": [], "summary_sent": False}


def _load() -> dict:
    data = load_json(STORE_PATH, _default())
    if not isinstance(data, dict):
        return _default()
    data.setdefault("queue", [])
    data.setdefault("active", None)
    data.setdefault("results", [])
    data.setdefault("summary_sent", False)
    return data


def _save(data: dict) -> None:
    # A recovery episode is small (at most the calendar jobs) and expires once
    # its one summary is sent, so no general-purpose history is needed here.
    atomic_write_json(STORE_PATH, data)


def _backend_uses_ollama(label: str) -> bool:
    task_name, _, kind = POLICIES[label]
    if kind == "none":
        return False
    return (resolve_backend(task_name) or "ollama").strip().lower() == "ollama"


def ollama_ready() -> bool:
    """A short startup probe; warming happens inside each selected task."""
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    try:
        response = requests.get(f"{host}/api/tags", timeout=3)
        response.raise_for_status()
        return True
    except Exception:
        return False


def _launchctl_output(label: str) -> str:
    result = subprocess.run(
        ["launchctl", "print", f"{DOMAIN}/{label}"], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def launch_status(label: str) -> dict:
    output = _launchctl_output(label)
    running = "state = running" in output
    exit_code = None
    for line in output.splitlines():
        if "last exit code =" in line:
            try:
                exit_code = int(line.rsplit("=", 1)[1].strip())
            except ValueError:
                pass
            break
    return {"running": running, "exit_code": exit_code}


def start(label: str) -> bool:
    return subprocess.run(
        ["launchctl", "kickstart", f"{DOMAIN}/{label}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode == 0


def enqueue(labels: list[str], now: datetime | None = None) -> dict:
    now = now or _now()
    unknown = [label for label in labels if label not in POLICIES]
    accepted = [label for label in labels if label in POLICIES]
    added = []
    with locked(STORE_PATH):
        data = _load()
        present = {job["label"] for job in data["queue"]}
        if data["active"]:
            present.add(data["active"]["label"])
        for label in accepted:
            if label in present:
                continue
            task_name, priority, _ = POLICIES[label]
            data["queue"].append({
                "label": label, "task_name": task_name, "priority": priority,
                "queued_at": now.isoformat(timespec="seconds"), "attempts": 0,
                "not_before": now.isoformat(timespec="seconds"),
            })
            added.append(label)
            present.add(label)
        if added:
            data["summary_sent"] = False
        _save(data)
    return {"added": added, "unknown": unknown}


def recovering_task(task_name: str, detail: object | None = None) -> bool:
    """Record a task's own failure while it is a catch-up run, suppressing its push."""
    with locked(STORE_PATH):
        data = _load()
        active = data.get("active")
        if not active or active.get("task_name") != task_name:
            return False
        if detail is not None:
            active.setdefault("failures", []).append(str(detail))
            _save(data)
        return True


def status() -> dict:
    """A small dashboard-safe view; no process or network access."""
    data = _load()
    return {
        "active": data.get("active"),
        "queued": [
            {k: job[k] for k in ("label", "task_name", "priority", "attempts", "not_before")}
            for job in data.get("queue", [])
        ],
        "results": data.get("results", []),
    }


def _append_result(data: dict, job: dict, result: str, detail: str = "") -> None:
    data["results"].append({
        "label": job["label"], "task_name": job["task_name"], "result": result,
        "detail": detail, "at": _now().isoformat(timespec="seconds"),
    })


def _summary(data: dict) -> str:
    completed = sum(row["result"] == "completed" for row in data["results"])
    failed = [row for row in data["results"] if row["result"] == "failed"]
    message = f"Startup recovery completed: {completed} task(s) caught up"
    if failed:
        message += "; failed: " + ", ".join(row["task_name"] for row in failed)
    return message + "."


def _finish_if_drained(data: dict) -> str | None:
    if data["active"] is None and not data["queue"] and data["results"] and not data["summary_sent"]:
        data["summary_sent"] = True
        return _summary(data)
    return None


def run_once(now: datetime | None = None) -> dict:
    """Advance exactly one catch-up job, returning a compact, testable outcome."""
    now = now or _now()
    summary = None
    with locked(STORE_PATH):
        data = _load()
        active = data.get("active")
        if active:
            probe = launch_status(active["label"])
            if probe["running"] or probe["exit_code"] is None:
                return {"status": "running", "label": active["label"]}
            data["active"] = None
            if probe["exit_code"] == 0:
                _append_result(data, active, "completed")
            else:
                attempts = active["attempts"] + 1
                detail = "; ".join(active.get("failures", [])) or f"exit {probe['exit_code']}"
                if attempts >= MAX_ATTEMPTS:
                    _append_result(data, active, "failed", detail)
                else:
                    active["attempts"] = attempts
                    active["not_before"] = (now + timedelta(seconds=RETRY_DELAYS_S[attempts - 1])).isoformat(timespec="seconds")
                    active.pop("failures", None)
                    data["queue"].append(active)
            summary = _finish_if_drained(data)
            _save(data)
            result = {"status": "finished", "label": active["label"], "exit_code": probe["exit_code"]}
        else:
            candidates = sorted(
                (job for job in data["queue"] if datetime.fromisoformat(job["not_before"]) <= now),
                key=lambda job: (job["priority"], job["queued_at"]),
            )
            ready = None
            ollama_is_ready = None
            for job in candidates:
                if not _backend_uses_ollama(job["label"]):
                    ready = job
                    break
                if ollama_is_ready is None:
                    ollama_is_ready = ollama_ready()
                if ollama_is_ready:
                    ready = job
                    break
            if ready is None:
                return {"status": "waiting_ollama" if candidates else "idle"}
            if launch_status(ready["label"])["running"]:
                return {"status": "already_running", "label": ready["label"]}
            if not start(ready["label"]):
                ready["attempts"] += 1
                if ready["attempts"] >= MAX_ATTEMPTS:
                    data["queue"].remove(ready)
                    _append_result(data, ready, "failed", "launchctl kickstart failed")
                    summary = _finish_if_drained(data)
                else:
                    ready["not_before"] = (now + timedelta(
                        seconds=RETRY_DELAYS_S[ready["attempts"] - 1]
                    )).isoformat(timespec="seconds")
                _save(data)
                result = {"status": "start_failed", "label": ready["label"]}
            else:
                data["queue"].remove(ready)
                ready["started_at"] = now.isoformat(timespec="seconds")
                data["active"] = ready
                _save(data)
                result = {"status": "started", "label": ready["label"]}
    if summary:
        from agent.tools.notify import notify
        notify(summary, title="Wren: startup recovery", priority="high", email_fallback=True)
        result["summary"] = summary
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enqueue", nargs="+", metavar="LABEL")
    args = parser.parse_args(argv)
    if args.enqueue:
        print(enqueue(args.enqueue))
        return 0
    logger = setup_logger("startup_recovery")
    try:
        result = run_once()
        # No run-boundary lines here, and none on an idle poll. This fires every
        # 60s and is idle essentially always — measured 5,474 of 5,474 polls —
        # and it is a daemon by chat/insights.py's own rule (StartInterval, no
        # StartCalendarInterval), so parse_runs skips it and AGENTS.md says to
        # leave the boundaries out. The dashboard reads this task's state from
        # status() instead. The three unconditional lines had no reader at all
        # and wrote 4,320 a day, half of them into the .launchd.log mirror that
        # nothing rotates and log_inspector deliberately skips: 1.0 MB in four
        # days, against the ~19 KB/day chat/logview.py states as expected.
        if result.get("status") != "idle":
            logger.info("startup recovery -> %s", result)
        return 0
    except Exception as e:
        logger.exception("startup recovery run failed: %s", e)
        notify_failure("startup_recovery", e, logger)
        return 1


if __name__ == "__main__":
    sys.exit(main())
