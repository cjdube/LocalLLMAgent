"""Log-viewer JSON API — the two endpoints /logs polls.

All reading and all path resolution live in chat/logview.py; this is the Flask
edge. Registered as a blueprint by chat/server.py, alongside the dashboard and
opportunities APIs.

Read-only by construction: nothing here opens a file for writing, and no route
takes a path. A log key is looked up in logview's catalogue or 404s, so a
request naming a file outside logs/ is an unknown key rather than a traversal.
"""

import logging

from flask import Blueprint, jsonify, request

from chat.auth import _authenticated
from chat.logview import DEFAULT_LIMIT, list_logs, read_log

logger = logging.getLogger("wren")

logs_bp = Blueprint("logs", __name__)


@logs_bp.route("/api/logs", methods=["GET"])
def api_logs():
    """The file picker: every readable log and its streams."""
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    return jsonify({"logs": list_logs()})


@logs_bp.route("/api/logs/entries", methods=["GET"])
def api_log_entries():
    """One page of entries, newest last.

    Query: key (required), stream=log|stdout, limit, before, after, level, q.

    `before` pages older, `after` polls for what's new (the live tail). Passing
    both is meaningless, so `after` wins — it's the one a poll sends.

    Like /api/run_stats, the numeric parameters are CLAMPED rather than
    validated: they are view state, and a nonsense value should narrow the page,
    not 400 the view that asked for it. `key` is the one exception — an unknown
    key is a 404, because silently showing a different file than the one asked
    for is worse than an error.
    """
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401

    key = request.args.get("key", "")
    stream = request.args.get("stream", "log")
    if stream not in ("log", "stdout"):
        stream = "log"
    before = request.args.get("before", type=int)
    after = request.args.get("after", type=int)
    data = read_log(
        key,
        stream=stream,
        limit=request.args.get("limit", type=int) or DEFAULT_LIMIT,
        before=before if before and before > 0 and after is None else None,
        after=after if after is not None and after >= 0 else None,
        level=request.args.get("level", ""),
        query=request.args.get("q", ""),
    )
    if data is None:
        return jsonify({"error": "unknown log"}), 404
    return jsonify(data)
