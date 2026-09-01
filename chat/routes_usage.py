"""Model-usage JSON API — the one endpoint /activity polls.

All reading and all aggregation live in chat/usage.py; this is the Flask edge,
registered as a blueprint by chat/server.py alongside the dashboard, logs and
opportunities APIs.

Read-only by construction: nothing here writes, and no route takes a path.
"""

import logging

from flask import Blueprint, jsonify, request

from chat.auth import _authenticated
from chat.usage import summarize

logger = logging.getLogger("wren")

usage_bp = Blueprint("usage", __name__)

# The page offers 7/30/90. The clamp is wider than the buttons on purpose — a
# hand-typed ?days=365 should answer, not 400 — but bounded, because the window
# is what keeps a year-old ledger from being parsed on every poll.
MIN_DAYS = 1
MAX_DAYS = 365
DEFAULT_DAYS = 7


@usage_bp.route("/api/usage", methods=["GET"])
def api_usage():
    """Token, cost and call totals across every instrumented agent.

    `days` is CLAMPED rather than validated, matching /api/run_stats: it is view
    state, and a nonsense value should narrow the window, not 500 the page that
    asked for it.
    """
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    days = request.args.get("days", type=int) or DEFAULT_DAYS
    days = max(MIN_DAYS, min(MAX_DAYS, days))
    return jsonify(summarize(days))
