"""Tests for chat/routes_nudges.py — the ntfy button's tap endpoint.

The action itself is tested in tests/test_nudge_actions.py; here, only what the
route adds: the token gate, POST only, and the acknowledging push.
"""

import os

os.environ.setdefault("WREN_CHAT_TOKEN", "test-token")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import pytest

from agent import nudge_actions as na
from chat import routes_nudges as rn
from chat import server as srv


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:  # no session: the phone has no cookie
        yield c


@pytest.fixture
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(rn, "notify", lambda **k: sent.append(k))
    return sent


def test_bad_token_is_403_and_does_nothing(client, pushes, monkeypatch):
    monkeypatch.setattr(na, "perform", lambda *a: pytest.fail("must not run"))
    assert client.post("/api/nudges/act?token=garbage").status_code == 403
    assert pushes == []


def test_get_is_not_allowed(client):
    assert client.get(f"/api/nudges/act?token={na.make_token('x')}").status_code == 405


def test_done_tap_pushes_an_ack(client, pushes, monkeypatch):
    monkeypatch.setattr(na, "perform", lambda nid: {"done": True, "row": {"action": "wiki"}})
    resp = client.post(f"/api/nudges/act?token={na.make_token('abc')}")
    assert resp.get_json() == {"ok": True}
    assert "wiki" in pushes[0]["message"]


def test_repeat_tap_is_ok_with_no_second_push(client, pushes, monkeypatch):
    monkeypatch.setattr(na, "perform", lambda nid: {"already": True, "row": {}})
    assert client.post(f"/api/nudges/act?token={na.make_token('abc')}").get_json()["already"]
    assert pushes == []


def test_failed_action_says_so(client, pushes, monkeypatch):
    monkeypatch.setattr(na, "perform", lambda nid: {"error": "ClickUp down"})
    assert client.post(f"/api/nudges/act?token={na.make_token('abc')}").status_code == 502
    assert "ClickUp down" in pushes[0]["message"]


# The /nudges page API: session-authenticated, unlike the tap above.

@pytest.fixture
def auth_client(client):
    with client.session_transaction() as sess:
        sess["authenticated"] = True
    return client


def test_page_api_needs_a_session(client, monkeypatch):
    monkeypatch.setattr(na, "perform", lambda *a: pytest.fail("must not run"))
    monkeypatch.setattr(na, "dismiss", lambda *a: pytest.fail("must not run"))
    assert client.get("/api/nudges").status_code == 401
    assert client.post("/api/nudges/abc/act").status_code == 401
    assert client.post("/api/nudges/abc/dismiss").status_code == 401


def test_page_lists_rows(auth_client, monkeypatch):
    monkeypatch.setattr(na, "all_rows", lambda: [{"id": "abc"}])
    assert auth_client.get("/api/nudges").get_json() == {"nudges": [{"id": "abc"}]}


def test_page_act_runs_the_same_action_with_no_push(auth_client, pushes, monkeypatch):
    seen = []
    monkeypatch.setattr(na, "perform",
                        lambda nid: seen.append(nid) or {"done": True, "row": {"action": "wiki"}})
    assert auth_client.post("/api/nudges/abc/act").status_code == 200
    assert seen == ["abc"] and pushes == []


def test_page_act_failure_is_502(auth_client, monkeypatch):
    monkeypatch.setattr(na, "perform", lambda nid: {"error": "ClickUp down"})
    resp = auth_client.post("/api/nudges/abc/act")
    assert resp.status_code == 502 and resp.get_json()["error"] == "ClickUp down"


def test_page_dismiss(auth_client, monkeypatch):
    monkeypatch.setattr(na, "dismiss", lambda nid: {"dismissed": True, "row": {"id": nid}})
    assert auth_client.post("/api/nudges/abc/dismiss").get_json()["dismissed"]
    monkeypatch.setattr(na, "dismiss", lambda nid: {"error": "that nudge has expired"})
    assert auth_client.post("/api/nudges/abc/dismiss").status_code == 400
