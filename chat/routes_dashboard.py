"""Dashboard / scheduler JSON API — the read-only endpoints the dashboard and
/map pages poll. All data comes from chat.insights (schedules, run history,
capabilities, system map), the memory store, and the ntfy health probe; none of
it touches the chat conversation state, so it lives apart from chat/server.py.

Registered as a Flask blueprint by chat/server.py. `run_manager` (the on-demand
"Run now" trigger) lives here with the routes that drive it."""

import logging

from flask import Blueprint, jsonify, request

from agent.toolset import TOOLS, WRITE_TOOLS
from agent.tools.memory import recall
from agent.tools.notify import ntfy_health
from tasks.startup_recovery import status as startup_recovery_status
from chat.auth import _authenticated
from chat.insights import (
    RunManager,
    _agent_of,
    describe_tools,
    discover_tasks,
    next_run,
    parse_run_detail,
    parse_runs,
    run_stats,
    system_map,
    task_by_key,
    vault_health,
)

logger = logging.getLogger("wren")

dashboard_bp = Blueprint("dashboard", __name__)

# Triggers scheduled tasks on demand for the dashboard's "Run now" button.
run_manager = RunManager()


def _run_summary(run: dict | None) -> dict | None:
    """The slice of a run the Overview needs — omits the heavy tool_calls/error."""
    if run is None:
        return None
    return {k: run[k] for k in ("id", "start", "end", "duration_s", "status", "summary")}


@dashboard_bp.route("/api/schedules", methods=["GET"])
def api_schedules():
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    out = []
    for task in discover_tasks():
        runs = [] if task["is_daemon"] else parse_runs(task["log_path"], limit=10)
        out.append({
            "key": task["key"],
            "display_name": task["display_name"],
            "human_schedule": task["human_schedule"],
            "is_daemon": task["is_daemon"],
            "external": task["external"],
            # Which agent owns the job — the dashboard groups by it, the same
            # way /map filters by it. Derived from the launchd Label, so a task
            # that moves between agents needs no table here.
            "agent": _agent_of(task),
            "next_run": next_run(task["schedule"]),
            "last_run": _run_summary(runs[0] if runs else None),
            "recent_statuses": [r["status"] for r in runs],
        })
    return jsonify({"tasks": out, "startup_recovery": startup_recovery_status()})


@dashboard_bp.route("/api/runs/<task_key>", methods=["GET"])
def api_runs(task_key: str):
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    task = task_by_key(task_key)
    if task is None:
        return jsonify({"error": "unknown task"}), 404
    runs = parse_runs(task["log_path"], limit=50)
    return jsonify({
        "task": {"key": task["key"], "display_name": task["display_name"],
                 "human_schedule": task["human_schedule"], "is_daemon": task["is_daemon"]},
        "runs": [_run_summary(r) for r in runs],
    })


@dashboard_bp.route("/api/runs/<task_key>/<run_id>", methods=["GET"])
def api_run_detail(task_key: str, run_id: str):
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    task = task_by_key(task_key)
    if task is None:
        return jsonify({"error": "unknown task"}), 404
    run = parse_run_detail(task["log_path"], run_id)
    if run is None:
        return jsonify({"error": "unknown run"}), 404
    return jsonify(run)


@dashboard_bp.route("/api/run_stats", methods=["GET"])
def api_run_stats():
    """Duration series per task for the dashboard's charts. /api/schedules
    carries only the last run and a bare status list, which is the "did it
    work" question; this is the "how long does it take" one."""
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    # Clamped rather than validated: ?days= is a chart window, and a nonsense
    # value should narrow the chart, not 400 the page that requested it.
    days = request.args.get("days", type=int) or 30
    return jsonify(run_stats(days=max(1, min(days, 365))))


@dashboard_bp.route("/api/health/ntfy", methods=["GET"])
def api_health_ntfy():
    """Live push-channel probe for the dashboard pill. The log inspector's
    check runs once at 8am and reports by email; this answers "is push up
    right now" — the question you have after restarting the ntfy container.

    Server reachability only, not token validity — see ntfy_health()."""
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    return jsonify(ntfy_health())


@dashboard_bp.route("/api/capabilities", methods=["GET"])
def api_capabilities():
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    return jsonify({"tools": describe_tools(TOOLS, WRITE_TOOLS)})


@dashboard_bp.route("/api/system_map", methods=["GET"])
def api_system_map():
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    return jsonify(system_map(TOOLS, WRITE_TOOLS))


@dashboard_bp.route("/api/vault_health", methods=["GET"])
def api_vault_health():
    """Is the learnings wiki in good shape — as opposed to /api/schedules, which
    says whether its jobs ran. A skipped source leaves a green run row."""
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    return jsonify(vault_health())


@dashboard_bp.route("/api/memories", methods=["GET"])
def api_memories():
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    data = recall()
    active = [m for m in data["memories"] if m.get("scope", "active") == "active"]
    archival = [m for m in data["memories"] if m.get("scope", "active") == "archival"]
    archival.sort(key=lambda m: m.get("access_count", 0), reverse=True)
    return jsonify({"active": active, "archival": archival})


@dashboard_bp.route("/api/run/<task_key>", methods=["POST"])
def api_run(task_key: str):
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    result = run_manager.start(task_key)
    logger.info(f"dashboard run-now {task_key} -> {result}")
    return jsonify(result), (200 if result.get("ok") else 409)


@dashboard_bp.route("/api/run/<task_key>/status", methods=["GET"])
def api_run_status(task_key: str):
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    if task_by_key(task_key) is None:
        return jsonify({"error": "unknown task"}), 404
    return jsonify(run_manager.status(task_key))
