"""The /api/imports JSON API — the chat page's paperclip button uploads a CSV
or PDF here; the model then lists and pages through it via agent/tools/imports.py's
own read-only tools.

Registered as a Flask blueprint by chat/server.py.
"""

import logging

from flask import Blueprint, jsonify, request

from agent.tools import imports
from chat.auth import _authenticated

logger = logging.getLogger("wren")

imports_bp = Blueprint("imports", __name__)

# The app-wide MAX_CONTENT_LENGTH is 256KB, sized for a chat turn — too small
# for a file upload. Scope the raise to this blueprint only: imports.py's own
# MAX_IMPORT_BYTES (5MiB) plus headroom for multipart encoding overhead
# (boundary markers, headers, base64-ish padding on the field), so a file right
# at the real limit isn't rejected by this outer cap before save_import() ever
# sees it and returns its own, more useful error.
MAX_IMPORTS_BODY_BYTES = imports.MAX_IMPORT_BYTES + 64 * 1024


@imports_bp.before_request
def _allow_larger_import_bodies():
    request.max_content_length = MAX_IMPORTS_BODY_BYTES


@imports_bp.route("/api/imports", methods=["POST"])
def api_upload_import():
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401

    file = request.files.get("file")
    if file is None or not file.filename:
        logger.warning("api_imports: upload rejected (no file field)")
        return jsonify({"error": "no file provided"}), 400

    data = file.read()
    result = imports.save_import(file.filename, data)
    if "error" in result:
        logger.warning("api_imports: upload rejected (%s)", result["error"])
        return jsonify(result), 400

    size = f"{result['pages']} pages" if result["kind"] == "pdf" else f"{result['rows']} rows"
    logger.info("api_imports: uploaded %r (%s, %d bytes)", result["name"], size, len(data))
    return jsonify(result)


@imports_bp.route("/api/imports", methods=["GET"])
def api_list_imports():
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    return jsonify(imports.list_imports())
