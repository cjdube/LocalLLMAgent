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
