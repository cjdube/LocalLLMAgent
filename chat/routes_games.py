"""The /games surface: the games page, its JSON API, and the serving side of
each game that Wren hosts.

A hosted game is two things behind Wren's login: its built browser bundle,
served straight off disk, and a proxy for the calls its own service answers.
Mounting it under Wren's origin rather than giving it a port of its own is
deliberate — the game has no authentication of its own worth exposing, and a
second listener on the tailnet would be a way into it that skips Wren's token.

How much of a service the proxy opens is decided per game: every game may reach
its AI endpoints, and only a game whose registry entry sets proxy_api may reach
the rest of its API. See game_api below and docs/games.md.

The registry itself is agent/tools/games.py, shared with the chat tool, so the
page and the model always list the same games.

Registered as a Flask blueprint by chat/server.py."""

import logging

import requests
from flask import Blueprint, jsonify, redirect, request, send_from_directory

from agent.tools.games import games, list_games
from chat.auth import _authenticated

logger = logging.getLogger("wren")

games_bp = Blueprint("games", __name__)

# How long to wait on the game's own service before giving up.
#
# These must stay ABOVE the browser's own budgets (150s per decision, 600s for
# warmup — see WeighAnchor src/agent/client.ts), because the game depends on the
# server being the side that gives up first: a browser-side abort does not stop
# an Ollama generation, so if this proxy timed out first the model would still be
# busy and the retry would queue behind it.
#
# Flask runs threaded, so a warmup parked here for ten minutes doesn't block the
# *server* — but it does block chat, one layer down: Ollama runs with
# OLLAMA_NUM_PARALLEL=1, so the generation holds the single slot and a chat turn
# started during a warmup queues behind it silently, looking like a hang rather
# than a wait. That is the real cost of raising these numbers. See
# docs/games.md ("Game turns and chat turns queue behind each other") and
# docs/ollama-serving.md (starvation).
AI_TIMEOUT_S = 160
WARMUP_TIMEOUT_S = 620

# A game's own state calls (its board, a move, the current view) are local and
# fast — nothing in them waits on a model. Kept short on purpose: a game service
# that has wedged should read as broken quickly, not park a request for minutes
# the way an AI call legitimately does.
GAME_API_TIMEOUT_S = 15

# The app-wide MAX_CONTENT_LENGTH is 256KB, sized for a chat turn. A game's
# batched log flush is bigger than that sounds: one state snapshot runs to ~14KB
# and a burst at round resolution flushes several at once, so the cap is
# reachable — and a 413 would be invisible, because the game treats logging as
# best-effort and only console.warns on failure. Match the game service's own
# 2MB body limit, scoped to these routes so chat keeps the tighter cap.
MAX_GAME_BODY_BYTES = 2 * 1024 * 1024

def _is_proxyable(endpoint: str) -> bool:
    """One flat path segment, so the proxied URL can only ever be
    /api/ai/<endpoint> on the game's own service.

    The route's <path:endpoint> converter admits "/" and "..", and requests()
    normalizes the dot segments away when it prepares the URL — so an endpoint of
    "../../internal/x" reaches the service as http://127.0.0.1:<port>/internal/x,
    i.e. ANY path on it, not just its AI ones. Verified 2026-08-19: Werkzeug
    routing passes the dots through untouched, and a non-browser client (curl,
    a script) never normalizes them away first the way a browser would.

    Deliberately a shape check and NOT an allowlist of endpoint names. The proxy
    is dumb on purpose (see game_api) — the game's service owns its own routes,
    and naming them here would be a second place for them to drift, so a game
    adding an endpoint would 404 until someone edited Wren. This bounds the URL
    without knowing anything about what the game answers.
    """
    return bool(endpoint) and "/" not in endpoint and endpoint not in (".", "..")


def _is_forwardable(rest: str) -> bool:
    """The same bound as _is_proxyable, but for a path of several segments.

    The wider proxy exists so a game whose whole match runs over its own HTTP API
    can be played through Wren, which means nested paths like
    games/<id>/actions have to pass. What must NOT pass is a path that climbs out
    of /api/: requests() normalizes dot segments when it prepares the URL, so a
    rest of "../internal/x" would reach the service as /internal/x. Rejecting
    "." and ".." segments outright is what keeps every forwarded URL under the
    service's own /api/ prefix.
    """
    segments = rest.split("/")
    return bool(rest) and all(seg not in ("", ".", "..") for seg in segments)


@games_bp.before_request
def _allow_larger_game_bodies():
    request.max_content_length = MAX_GAME_BODY_BYTES


def _game(game_id: str) -> dict | None:
    return next((g for g in games() if g["id"] == game_id), None)


@games_bp.route("/api/games", methods=["GET"])
def api_games():
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    return jsonify(list_games())


@games_bp.route("/games/<game_id>/", methods=["GET"])
@games_bp.route("/games/<game_id>/<path:asset>", methods=["GET"])
def game_asset(game_id: str, asset: str = "index.html"):
    """Serve a game's built bundle. send_from_directory is what confines this to
    the game's own dist directory — it rejects traversal out of it."""
    if not _authenticated():
        # A browser navigation, not an XHR: bounce to the login page rather than
        # answering a JSON 401 the user would see as raw text. "/" renders the
        # login form; redirecting avoids importing LOGIN_PAGE from chat.server,
        # which would be a circular import.
        return redirect("/")
    game = _game(game_id)
    if game is None:
        return jsonify({"error": f"no game with id {game_id!r}"}), 404
    dist = game["dist"]
    if not dist.is_dir():
        # The bundle is a build artifact of another repo, so "missing" is the
        # normal state until someone builds it — say which directory and stop,
        # rather than 404ing every asset and looking like a routing bug.
        logger.warning(f"games: {game_id} not built, no dist at {dist}")
        return jsonify({"error": f"{game['name']} is not built yet (no {dist})"}), 503
    return send_from_directory(dist, asset)


@games_bp.route("/games/<game_id>/api/<path:rest>", methods=["GET", "POST"])
def game_api(game_id: str, rest: str):
    """Proxy one call through to the game's own service on loopback.

    Deliberately dumb: it forwards the body and hands back the response. The
    game's service owns prompt construction, schema validation, its own auth and
    its own fallbacks — duplicating any of that here would be a second place for
    it to drift.

    Two kinds of call come through here, and they are gated differently.

    The AI calls (/api/ai/<endpoint>) are open to every game: one flat segment,
    POST only, and the long model timeouts above. That is all Weigh Anchor's
    browser ever asks for.

    Everything else under /api/ is the game's own match API — several segments,
    GET as well as POST, and a bearer token the service issues to the seat. A
    game only gets that when its registry entry sets proxy_api, because opening
    a service's whole API to the browser is a decision per game, not a default:
    Weigh Anchor's service has routes its screen has no business calling.
    """
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    game = _game(game_id)
    if game is None:
        return jsonify({"error": f"no game with id {game_id!r}"}), 404

    is_ai = rest == "ai" or rest.startswith("ai/")
    if is_ai:
        endpoint = rest[len("ai/"):]
        if request.method != "POST" or not _is_proxyable(endpoint):
            # 404 rather than 400: from the caller's side, an endpoint this proxy
            # won't forward is one that doesn't exist.
            logger.warning(f"games: {game_id} rejected AI endpoint {endpoint!r}")
            return jsonify({"error": f"invalid AI endpoint {endpoint!r}"}), 404
        timeout = WARMUP_TIMEOUT_S if endpoint == "warmup" else AI_TIMEOUT_S
    else:
        if not game.get("proxy_api") or not _is_forwardable(rest):
            logger.warning(f"games: {game_id} rejected API path {rest!r}")
            return jsonify({"error": f"invalid API path {rest!r}"}), 404
        timeout = GAME_API_TIMEOUT_S

    url = f"http://127.0.0.1:{game['api_port']}/api/{rest}"
    # Only the bearer token is carried over. The service decides what a seat may
    # do from that token alone, and forwarding anything else — Host, Origin,
    # Cookie — would either leak Wren's session downstream or trip the game's own
    # loopback-name check, since requests() sets Host from the URL above.
    headers = {}
    token = request.headers.get("Authorization")
    if token:
        headers["Authorization"] = token

    try:
        if request.method == "GET":
            resp = requests.get(url, headers=headers, timeout=timeout)
        else:
            resp = requests.post(
                url, json=request.get_json(silent=True) or {}, headers=headers, timeout=timeout
            )
    except requests.RequestException as e:
        # Degrade with a readable message: the game's client surfaces the status
        # and body, and "connection refused" tells the user the service is down
        # far better than a bare 500 would.
        logger.warning(f"games: {game_id} {rest} proxy failed: {e}")
        return jsonify({"error": f"{game['name']} service unreachable: {e}"}), 502
    return resp.content, resp.status_code, {"Content-Type": resp.headers.get("Content-Type", "application/json")}
