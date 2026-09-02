"""Opportunities JSON API — the /opportunities page's triage endpoints (list,
mark interested/dismissed, watch/unwatch, and kick off research). Each mutating
route calls the same agent.tools.opportunities store the chat's own tools use,
so the page and chat can't drift apart.

Registered as a Flask blueprint by chat/server.py. `_start_research` lives here
with the route that triggers it; it spawns a daemon thread, so tests must stub
it on THIS module (see tests/test_server.py:research_spy)."""

import logging
import threading

from flask import Blueprint, jsonify, request

from agent.tools import opportunities
from agent.tools.notify import notify
from agent.tools.research import research_opportunity
from chat.auth import _authenticated

logger = logging.getLogger("wren")

opportunities_bp = Blueprint("opportunities", __name__)

# Ollama serves one request at a time (OLLAMA_NUM_PARALLEL=1), so concurrent
# research calls do not run in parallel — they queue, and an interactive chat
# turn queues behind all of them. Tapping "Interested" down a page of ten
# opportunities used to put ten model calls in that queue in a few seconds;
# chat then failed with a bare ReadTimeout at 120s, the 2026-08-03 incident
# shape, reachable from the phone in a few taps.
#
# One at a time. The work still all happens — the user asked for ten briefs —
# but a chat turn now waits behind at most one of them instead of ten. The
# threads holding this are idle, and each has already written its "pending"
# marker, so the page reads correctly the whole time.
_RESEARCH_SLOT = threading.Semaphore(1)


def _start_research(item: dict) -> None:
    """Kick off the research pipeline for one opportunity on a daemon thread —
    a couple of Tavily searches plus a local-model summary takes a minute or
    two, far too long to hold the page's request open. The pending marker is
    written synchronously so the page shows "researching…" on its next load;
    the thread overwrites it with done/failed and pings the phone (the whole
    point of async: the user has usually navigated away by the time it lands).

    Threads run one at a time — see _RESEARCH_SLOT above."""
    opportunities.set_research(item["id"], {"status": "pending", "summary": None})

    def run():
        with _RESEARCH_SLOT:
            _research_one(item)

    threading.Thread(target=run, daemon=True, name=f"research-{item['id']}").start()


def _research_one(item: dict) -> None:
    """The body of one research thread, holding _RESEARCH_SLOT."""
    result = research_opportunity(item["id"])
    if "error" in result:
        logger.warning(f"research {item['id']} failed: {result['error']}")
        notify(title="Wren: research failed", message=f"{item['company']}: {result['error']}")
    else:
        logger.info(f"research {item['id']} done")
        notify(title="Wren: research ready",
               message=f"{item['company']} brief is on the opportunities page.")


@opportunities_bp.route("/api/opportunities", methods=["GET"])
def api_opportunities():
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    return jsonify({"items": opportunities.all_items(),
                    "watchlist": opportunities.get_watchlist()})


@opportunities_bp.route("/api/opportunities/<item_id>/status", methods=["POST"])
def api_opportunity_status(item_id: str):
    """Triage from the /opportunities page — same store call the chat's
    update_opportunity tool makes, so the two surfaces can't drift apart.
    Marking an item interested auto-starts research on it (once)."""
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    status = (request.get_json(silent=True) or {}).get("status", "")
    result = opportunities.update_opportunity(item_id, status)
    logger.info(f"opportunities page: {item_id} -> {status!r}: {result}")
    if "error" not in result and status == "interested":
        item = opportunities.get_item(item_id)
        if item is not None and not item.get("research"):
            _start_research(item)
    return jsonify(result), (200 if "error" not in result else 400)


@opportunities_bp.route("/api/opportunities/<item_id>/research", methods=["POST"])
def api_opportunity_research(item_id: str):
    """The page's manual Research button (also the retry after a failure)."""
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    item = opportunities.get_item(item_id)
    if item is None:
        return jsonify({"error": f"no opportunity with id {item_id!r}"}), 400
    if (item.get("research") or {}).get("status") == "pending":
        return jsonify({"status": "pending", "note": "already researching"})
    _start_research(item)
    return jsonify({"status": "pending"}), 202


@opportunities_bp.route("/api/opportunities/watchlist", methods=["POST"])
def api_watch_company():
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    body = request.get_json(silent=True) or {}
    result = opportunities.watch_company(
        body.get("company", ""), body.get("ats", ""), body.get("slug", ""))
    logger.info(f"opportunities page: watch {body}: {result}")
    return jsonify(result), (200 if "error" not in result else 400)


@opportunities_bp.route("/api/opportunities/watchlist/<watch_id>", methods=["DELETE"])
def api_unwatch_company(watch_id: str):
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    result = opportunities.unwatch_company(watch_id)
    logger.info(f"opportunities page: unwatch {watch_id}: {result}")
    return jsonify(result), (200 if "error" not in result else 400)
