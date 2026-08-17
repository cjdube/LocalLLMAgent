"""The /wiki JSON API — lint findings and single-page reads.

The Flask edge only. Running the sibling repo's lint lives in chat/wikilint.py,
and reading the vault lives in agent/tools/wiki.py; this module does auth,
shapes, and status codes.

The lint's `fix` endpoint is the one write path here, and it is a POST for that
reason. It cannot take a target: it runs the sibling's apply_safe_fixes, whose
whole contract is that it touches only the mechanical subset (self-links, dead
index entries) and leaves every judgment call alone. Nothing the caller sends
widens that.

Page reads are bounded by the vault, not by a caller-supplied path: the name
goes through _safe_child, which rejects anything resolving outside wiki/.

Registered as a Flask blueprint by chat/server.py. The /wiki and /wiki/lint HTML
pages are served from there with the other views, as chat/routes_games.py does.
"""

import logging

from flask import Blueprint, jsonify

from agent.tools.wiki import _require_vault, _safe_child
from chat.auth import _authenticated
# The module, not the function: tests/conftest.py blocks the lint subprocess by
# replacing wikilint.run_lint, and a `from ... import run_lint` here would bind
# the real one at import time and walk straight past that guard.
from chat import wikigraph, wikilint

logger = logging.getLogger("wren")

wiki_bp = Blueprint("wiki", __name__)


@wiki_bp.route("/api/wiki/lint", methods=["GET"])
def api_wiki_lint():
    """The structural findings, cached on the vault's mtimes so the view can
    re-check freely. Never 500s: a missing or broken lint repo comes back as
    {"error": ...} with a 200, because the page renders that as its own state."""
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    return jsonify(wikilint.run_lint())


@wiki_bp.route("/api/wiki/lint/fix", methods=["POST"])
def api_wiki_lint_fix():
    """Apply the safe, mechanical fixes, then return the refreshed findings.

    Logged at INFO with the change list — this is the only thing in Wren that
    writes to the learnings vault, and a vault edit with no record of who made
    it is indistinguishable from an ingest bug."""
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    result = wikilint.run_lint(fix=True)
    if "error" in result:
        logger.warning("api_wiki_lint_fix: %s", result["error"])
    else:
        logger.info("api_wiki_lint_fix: applied %d fix(es): %s",
                    len(result["fixes"]), "; ".join(result["fixes"]) or "none")
    return jsonify(result)


@wiki_bp.route("/api/wiki/graph", methods=["GET"])
def api_wiki_graph():
    """The whole vault as nodes and edges, for the /wiki explorer.

    Sent in one payload rather than paged: the view is a force layout, and a
    partial graph doesn't lay out — nodes would jump every time more arrived.
    It is ~110 KB over a local network, and it's cached on the vault's mtimes,
    so the cost is bounded and paid once per edit."""
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    return jsonify(wikigraph.build_graph())


@wiki_bp.route("/api/wiki/page/<name>", methods=["GET"])
def api_wiki_page(name):
    """One wiki page, in full.

    Deliberately not agent/tools/wiki.py's read_wiki_page: that one applies
    _fit_page, which is a budget for the model's context window. A human reading
    the page on their phone has no such budget, and a silently trimmed page is
    the exact failure that tool's docstring is about."""
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    vault, err = _require_vault()
    if err:
        return jsonify(err), 404
    filename = name if name.endswith(".md") else f"{name}.md"
    try:
        path = _safe_child(vault / "wiki", filename)
    except ValueError:
        return jsonify({"error": f"'{name}' is not a page in this wiki"}), 400
    if not path.is_file():
        return jsonify({"error": f"wiki page '{name}' not found"}), 404
    return jsonify({"name": name, "content": path.read_text(encoding="utf-8", errors="replace")})
