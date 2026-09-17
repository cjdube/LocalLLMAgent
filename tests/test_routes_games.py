"""Tests for the games blueprint: auth gating, serving a game's built bundle,
and the proxy to a game's own service.

The proxy is the security-relevant part — it is the one route that forwards a
request to another local service — so the auth gate, the timeout split, and the
failure path get explicit coverage. It carries two gates that are easy to
confuse: every game may reach /api/ai/<one-segment> by POST, and only a game
whose registry entry sets proxy_api may reach the rest of /api/. Both halves are
asserted below, in each direction. requests.post and requests.get are
monkeypatched throughout; no test may reach a real game service.
"""

import os

os.environ.setdefault("WREN_CHAT_TOKEN", "test-token")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import pytest
import requests

from chat import routes_games
from chat import server as srv


@pytest.fixture
def client():
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:
        yield c


@pytest.fixture
def auth_client(client):
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["sid"] = "test-sid"
    return client


@pytest.fixture
def built(tmp_path, monkeypatch):
    """A built bundle on disk, with an index and one asset."""
    dist = tmp_path / "wa" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Weigh Anchor</title>")
    (dist / "assets" / "index.js").write_text("console.log('game');")
    monkeypatch.setenv("WEIGH_ANCHOR_DIR", str(tmp_path / "wa"))
    return dist


@pytest.fixture
def posted(monkeypatch):
    """Captures the proxied call and returns a canned 200."""
    calls = []

    class Resp:
        status_code = 200
        content = b'{"ok": true}'
        headers = {"Content-Type": "application/json"}

    def fake_post(url, json=None, timeout=None, headers=None):
        calls.append({
            "method": "POST", "url": url, "json": json,
            "timeout": timeout, "headers": headers or {},
        })
        return Resp()

    def fake_get(url, timeout=None, headers=None):
        calls.append({
            "method": "GET", "url": url, "json": None,
            "timeout": timeout, "headers": headers or {},
        })
        return Resp()

    monkeypatch.setattr(routes_games.requests, "post", fake_post)
    monkeypatch.setattr(routes_games.requests, "get", fake_get)
    return calls


# --------------------------------------------------------------------------- #
# Auth gating
# --------------------------------------------------------------------------- #

def test_api_games_requires_auth(client):
    resp = client.get("/api/games")
    assert resp.status_code == 401


def test_games_page_shows_login_when_unauthenticated(client):
    resp = client.get("/games")
    assert b"token" in resp.data.lower()


def test_ai_proxy_requires_auth(client, posted):
    resp = client.post("/games/weigh-anchor/api/ai/build-row", json={"view": {}})
    assert resp.status_code == 401
    assert posted == [], "an unauthenticated request must not reach the game service"


def test_bundle_redirects_to_login_when_unauthenticated(client, built):
    # A browser navigation, not an XHR: a raw JSON 401 would be shown as text.
    resp = client.get("/games/weigh-anchor/")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"


# --------------------------------------------------------------------------- #
# Serving the bundle
# --------------------------------------------------------------------------- #

def test_serves_index_at_the_mount_root(auth_client, built):
    resp = auth_client.get("/games/weigh-anchor/")
    assert resp.status_code == 200
    assert b"Weigh Anchor" in resp.data


def test_serves_a_nested_asset(auth_client, built):
    # The built index references /games/weigh-anchor/assets/..., so this path
    # shape is exactly what the browser asks for.
    resp = auth_client.get("/games/weigh-anchor/assets/index.js")
    assert resp.status_code == 200
    assert b"console.log" in resp.data


def test_unbuilt_game_says_so_rather_than_404ing(auth_client):
    # conftest points WEIGH_ANCHOR_DIR at a directory that doesn't exist.
    resp = auth_client.get("/games/weigh-anchor/")
    assert resp.status_code == 503
    assert "not built" in resp.get_json()["error"]


def test_unknown_game_is_404(auth_client):
    assert auth_client.get("/games/no-such-game/").status_code == 404


def test_traversal_out_of_dist_is_refused(auth_client, built, tmp_path):
    (tmp_path / "secret.txt").write_text("not yours")
    resp = auth_client.get("/games/weigh-anchor/../../secret.txt")
    assert resp.status_code in (404, 403, 308)
    assert b"not yours" not in resp.data


# --------------------------------------------------------------------------- #
# The AI proxy
# --------------------------------------------------------------------------- #

def test_proxy_forwards_endpoint_and_body(auth_client, posted, monkeypatch):
    monkeypatch.setenv("WEIGH_ANCHOR_PORT", "3002")
    resp = auth_client.post("/games/weigh-anchor/api/ai/build-row", json={"view": {"seat": 1}})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert posted[0]["url"] == "http://127.0.0.1:3002/api/ai/build-row"
    assert posted[0]["json"] == {"view": {"seat": 1}}


def test_proxy_dials_the_configured_port(auth_client, posted, monkeypatch):
    monkeypatch.setenv("WEIGH_ANCHOR_PORT", "4111")
    auth_client.post("/games/weigh-anchor/api/ai/read", json={})
    assert posted[0]["url"].startswith("http://127.0.0.1:4111/")


def test_warmup_gets_the_long_timeout(auth_client, posted):
    # Warmup covers a cold model load (a documented 70-85s, observed higher).
    auth_client.post("/games/weigh-anchor/api/ai/warmup", json={})
    assert posted[0]["timeout"] == routes_games.WARMUP_TIMEOUT_S


def test_decisions_get_the_shorter_timeout(auth_client, posted):
    auth_client.post("/games/weigh-anchor/api/ai/pick-signal", json={})
    assert posted[0]["timeout"] == routes_games.AI_TIMEOUT_S


def test_proxy_timeouts_exceed_the_browsers_own_budgets():
    # The game depends on the SERVER being the side that gives up first: a
    # browser-side abort doesn't stop an Ollama generation, so a proxy that
    # timed out first would leave the model busy and the retry queued behind it.
    # The browser's budgets are 150s per decision and 600s for warmup.
    assert routes_games.AI_TIMEOUT_S > 150
    assert routes_games.WARMUP_TIMEOUT_S > 600


def test_unreachable_service_degrades_to_502(auth_client, monkeypatch):
    def refuse(*a, **k):
        raise requests.ConnectionError("connection refused")
    monkeypatch.setattr(routes_games.requests, "post", refuse)
    resp = auth_client.post("/games/weigh-anchor/api/ai/read", json={})
    assert resp.status_code == 502
    assert "unreachable" in resp.get_json()["error"]


def test_proxy_passes_the_services_status_through(auth_client, monkeypatch):
    class Resp:
        status_code = 400
        content = b'{"error": "log requires { gameId, events[] }"}'
        headers = {"Content-Type": "application/json"}

    monkeypatch.setattr(routes_games.requests, "post", lambda *a, **k: Resp())
    resp = auth_client.post("/games/weigh-anchor/api/ai/log", json={})
    assert resp.status_code == 400


def test_proxy_to_unknown_game_is_404(auth_client, posted):
    resp = auth_client.post("/games/no-such-game/api/ai/read", json={})
    assert resp.status_code == 404
    assert posted == []


def test_proxy_refuses_to_escape_the_ai_path(auth_client, posted):
    # <path:endpoint> admits "/" and "..", and requests() normalizes the dot
    # segments when it prepares the URL — so without this guard
    # "../../internal/x" would proxy the POST to http://127.0.0.1:3002/internal/x,
    # any path on the game's service rather than just its AI ones. A browser
    # would normalize the path before sending; curl or a script would not.
    for endpoint in ("../../internal/admin", "x/../../admin", "a/b", ".."):
        resp = auth_client.post(f"/games/weigh-anchor/api/ai/{endpoint}", json={})
        assert resp.status_code == 404, endpoint
    # Nothing reached the game's service.
    assert posted == []


def test_proxy_still_forwards_any_flat_endpoint_name(auth_client, posted):
    # The guard is a shape check, not an allowlist of names: the game's service
    # owns its own routes, so a new one there must not need an edit here.
    auth_client.post("/games/weigh-anchor/api/ai/some-new-endpoint", json={})
    assert posted[0]["url"] == "http://127.0.0.1:3002/api/ai/some-new-endpoint"


def test_game_routes_accept_bodies_larger_than_the_chat_cap(auth_client, posted):
    # A batched log flush runs well past the app-wide 256KB chat limit — one
    # state snapshot is ~14KB and a burst flushes several. A 413 here would be
    # invisible, because the game only console.warns when logging fails.
    assert routes_games.MAX_GAME_BODY_BYTES > srv.app.config["MAX_CONTENT_LENGTH"]
    big = {"gameId": "g1", "events": [{"kind": "state", "blob": "x" * 400_000}]}
    resp = auth_client.post("/games/weigh-anchor/api/ai/log", json=big)
    assert resp.status_code == 200


def test_chat_keeps_the_tighter_body_cap(auth_client):
    # The larger limit is scoped to the games blueprint; chat must not inherit it.
    resp = auth_client.post("/chat", json={"message": "x" * 400_000})
    assert resp.status_code == 413


# --------------------------------------------------------------------------- #
# The wider match-API proxy (proxy_api games)
# --------------------------------------------------------------------------- #
# Train Game runs its whole match over its own HTTP API from the browser, so the
# screen needs GET as well as POST, nested paths, and the bearer token the
# service issues to the seat. That is a bigger opening than the AI proxy, so it
# is per-game: the registry entry has to ask for it.

def test_match_api_forwards_a_nested_get_with_the_seat_token(auth_client, posted, monkeypatch):
    monkeypatch.setenv("TRAIN_GAME_PORT", "4173")
    resp = auth_client.get(
        "/games/train-game/api/games/g1/view",
        headers={"Authorization": "Bearer seat-token"},
    )
    assert resp.status_code == 200
    assert posted[0]["method"] == "GET"
    assert posted[0]["url"] == "http://127.0.0.1:4173/api/games/g1/view"
    # Without the token the service answers 401 to every match call, so a proxy
    # that dropped it would look like a broken game rather than a broken proxy.
    assert posted[0]["headers"]["Authorization"] == "Bearer seat-token"


def test_match_api_forwards_a_post_body(auth_client, posted):
    resp = auth_client.post(
        "/games/train-game/api/games/g1/actions",
        json={"expectedRevision": 4, "action": {"kind": "claim"}},
    )
    assert resp.status_code == 200
    assert posted[0]["url"] == "http://127.0.0.1:4173/api/games/g1/actions"
    assert posted[0]["json"] == {"expectedRevision": 4, "action": {"kind": "claim"}}


def test_match_api_uses_the_short_timeout(auth_client, posted):
    # Nothing in a match call waits on a model, so a wedged service should read
    # as broken quickly rather than parking a request for minutes.
    auth_client.get("/games/train-game/api/board")
    assert posted[0]["timeout"] == routes_games.GAME_API_TIMEOUT_S
    assert routes_games.GAME_API_TIMEOUT_S < routes_games.AI_TIMEOUT_S


def test_match_api_is_refused_for_a_game_that_did_not_ask_for_it(auth_client, posted):
    """The other half of the guarantee: opening a service's whole API is a
    decision per game. Weigh Anchor's entry sets proxy_api False, and its service
    has routes its screen has no business calling."""
    for method, path in [
        ("get", "/games/weigh-anchor/api/board"),
        ("post", "/games/weigh-anchor/api/games/g1/actions"),
        ("get", "/games/weigh-anchor/api/session"),
    ]:
        resp = getattr(auth_client, method)(path)
        assert resp.status_code == 404, path
    assert posted == [], "nothing may reach a game that did not opt in"


def test_match_api_requires_auth(client, posted):
    resp = client.get("/games/train-game/api/session")
    assert resp.status_code == 401
    assert posted == []


def test_match_api_refuses_to_climb_out_of_the_api_prefix(auth_client, posted):
    # requests() normalizes dot segments when it prepares the URL, so without the
    # guard "../internal/x" would reach the service as /internal/x — any path on
    # it, not just its API. A browser normalizes first; curl or a script does not.
    for rest in ("../internal/admin", "games/../../admin", "..", "a/./b", "a//b"):
        resp = auth_client.get(f"/games/train-game/api/{rest}")
        assert resp.status_code in (404, 301, 308), rest
    assert posted == []


def test_ai_path_still_refuses_get_for_every_game(auth_client, posted):
    # The AI proxy was POST-only before this route also learned GET, and the
    # game services treat those endpoints as commands. Widening the route must
    # not have widened that.
    assert auth_client.get("/games/weigh-anchor/api/ai/read").status_code == 404
    assert auth_client.get("/games/train-game/api/ai/read").status_code == 404
    assert posted == []


def test_train_game_is_registered_and_listed(auth_client):
    from agent.tools.games import list_games
    ids = [g["id"] for g in list_games()["games"]]
    assert "train-game" in ids
    assert "weigh-anchor" in ids
