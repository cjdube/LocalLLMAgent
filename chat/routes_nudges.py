"""The tap endpoint behind a daily-synthesis nudge's ntfy button.

Token-authenticated, NOT session-authenticated, like /api/bg/resolve: the
phone's ntfy app calls it directly, without the login cookie. The token is
HMAC-signed and names one stored nudge; the action itself, and single-use, live
in agent/nudge_actions.py. POST only, for the reasons bg_resolve gives.
"""

import logging

from flask import Blueprint, jsonify, request

from agent import nudge_actions
from agent.tools.notify import notify

logger = logging.getLogger("wren")

nudges_bp = Blueprint("nudges", __name__)

_DONE = {"wiki": "Saved to the wiki queue — it lands on the next ingest run.",
         "ticket": "Added as a comment on the ClickUp item."}


@nudges_bp.route("/api/nudges/act", methods=["POST"])
def nudge_act():
    nudge_id = nudge_actions.read_token(request.args.get("token", ""))
    if nudge_id is None:
        logger.warning("nudge_act: rejected invalid or expired token")
        return jsonify({"error": "invalid or expired token"}), 403
    result = nudge_actions.perform(nudge_id)
    logger.info("nudge_act nudge=%s result=%s", nudge_id,
                "done" if result.get("done") else
                "already" if result.get("already") else result.get("error"))
    # The ntfy button has no selected state, so the push is the only way the
    # phone shows what the tap did. A repeat tap gets no second push.
    if result.get("done"):
        notify(title="Wren: done", message=_DONE[result["row"]["action"]])
        return jsonify({"ok": True})
    if result.get("already"):
        return jsonify({"ok": True, "already": True})
    notify(title="Wren: that did not work", message=f"Nudge action failed: {result['error']}")
    return jsonify({"error": result["error"]}), 502
