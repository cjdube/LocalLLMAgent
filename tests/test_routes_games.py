"""Tests for the games blueprint: auth gating, serving a game's built bundle,
and the AI proxy.

The proxy is the security-relevant part — it is the one route that forwards a
request body to another local service — so the auth gate, the timeout split, and
the failure path get explicit coverage. requests.post is monkeypatched
throughout; no test may reach a real game service.
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

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return Resp()

    monkeypatch.setattr(routes_games.requests, "post", fake_post)
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
