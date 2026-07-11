"""Tests for chat.server's stateful, security-relevant HTTP paths.

Covers auth gating on every endpoint, the login throttle's 429, the
email-body-preview surfaced in confirmations, and the two subtle flows that
keep conversation history well-formed: declining a pending write when the user
sends a new message instead of answering, and rolling back a failed turn.

The model and network are never touched — chat.server.advance / resolve are
monkeypatched. Importing chat.server runs its module-level secret check, so the
two required secrets are stubbed into the environment before the import.
"""

import os
import threading

os.environ.setdefault("WREN_CHAT_TOKEN", "test-token")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import pytest

from agent.tools import memory
from chat import server as srv


@pytest.fixture
def client():
    srv.app.config["TESTING"] = True
    srv.conversations.clear()
    srv.pending_confirmations.clear()
    srv.cancel_events.clear()
    with srv.app.test_client() as c:
        yield c


@pytest.fixture
def auth_client(client):
    """A client with an authenticated session pinned to a known sid, so tests
    can read/seed chat.server.conversations[SID] directly."""
    with client.session_transaction() as sess:
        sess["authenticated"] = True
        sess["sid"] = "test-sid"
    return client


SID = "test-sid"
EMAIL_CALL = {"function": {"name": "send_email", "arguments": {"subject": "Hi", "body": "the body"}}}


# --------------------------------------------------------------------------- #
# Auth gating
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("method,path,kwargs", [
    ("post", "/chat", {"json": {"message": "hi"}}),
    ("post", "/chat/confirm", {"json": {"approved": True}}),
    ("post", "/chat/cancel", {}),
    ("post", "/chat/new", {}),
    ("get", "/api/schedules", {}),
    ("get", "/api/runs/morning_brief", {}),
    ("get", "/api/runs/morning_brief/someid", {}),
    ("get", "/api/capabilities", {}),
    ("post", "/api/run/morning_brief", {}),
    ("get", "/api/run/morning_brief/status", {}),
    ("get", "/api/memories", {}),
    ("get", "/api/system_map", {}),
])
def test_endpoints_require_auth(client, method, path, kwargs):
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "not authenticated"


def test_security_headers_present(client):
    resp = client.get("/")
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]


# --------------------------------------------------------------------------- #
# Login throttle (integration through /login)
# --------------------------------------------------------------------------- #

def test_login_throttle_returns_429_after_repeated_failures(client):
    ip = {"X-Forwarded-For": "203.0.113.7"}  # unique key, isolated from other tests
    for _ in range(srv.LoginThrottle.MAX_FAILURES):
        resp = client.post("/login", data={"token": "wrong"}, headers=ip)
        assert resp.status_code == 401
    resp = client.post("/login", data={"token": "wrong"}, headers=ip)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


# --------------------------------------------------------------------------- #
# History trimming — the budget that keeps the prompt inside num_ctx
# --------------------------------------------------------------------------- #

def _turn(i, size=100):
    """One complete user-turn: user msg, assistant tool_call, tool result, answer."""
    return [
        {"role": "user", "content": f"question {i} " + "q" * size},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "fetch_weather", "arguments": {"n": i}}}]},
        {"role": "tool", "content": "t" * size},
        {"role": "assistant", "content": f"answer {i} " + "a" * size},
    ]


def test_trim_history_is_a_noop_under_budget():
    history = [{"role": "system", "content": "s"}, *_turn(1)]
    before = list(history)
    assert srv._trim_history(history) == 0
    assert history == before


def test_trim_drops_oldest_whole_turns_keeps_system_and_last(monkeypatch):
    monkeypatch.setattr(srv, "MAX_HISTORY_CHARS", 400)
    history = [{"role": "system", "content": "s"}, *_turn(1), *_turn(2), *_turn(3)]

    dropped = srv._trim_history(history)

    assert dropped == 8  # turns 1 and 2, four messages each
    assert history[0]["role"] == "system"
    # What survives starts at a user-message boundary: no orphaned tool result
    # or assistant half-turn at the front of the retained window.
    assert history[1]["role"] == "user" and "question 3" in history[1]["content"]
    assert [m["role"] for m in history[1:]] == ["user", "assistant", "tool", "assistant"]


def test_trim_never_drops_the_only_turn(monkeypatch):
    monkeypatch.setattr(srv, "MAX_HISTORY_CHARS", 10)  # everything is over budget
    history = [{"role": "system", "content": "s"}, *_turn(1)]
    assert srv._trim_history(history) == 0
    assert len(history) == 5


def test_chat_trims_before_running_the_turn(auth_client, monkeypatch):
    monkeypatch.setattr(srv, "MAX_HISTORY_CHARS", 400)
    srv.conversations[SID] = [{"role": "system", "content": "s"},
                              *_turn(1), *_turn(2), *_turn(3)]

    def fake_advance(messages, tools, dispatch, confirm_before=frozenset(), logger=None,
                     should_cancel=None):
        return {"type": "final", "text": "done"}

    monkeypatch.setattr(srv, "advance", fake_advance)
    resp = auth_client.post("/chat", json={"message": "next question"})
    assert resp.status_code == 200

    history = srv.conversations[SID]
    assert history[0]["role"] == "system"
    contents = " ".join(m.get("content") or "" for m in history)
    assert "question 1" not in contents and "question 2" not in contents
    assert "question 3" in contents and "next question" in contents


# --------------------------------------------------------------------------- #
# Confirmation payload (describers themselves are covered in test_toolset.py)
# --------------------------------------------------------------------------- #

def test_call_response_confirm_carries_summary_and_detail():
    resp = srv._call_response({"type": "confirm", "call": EMAIL_CALL})
    assert resp["type"] == "confirm"
    assert resp["summary"].startswith("Send an email")
    assert resp["detail"] == "the body"


# --------------------------------------------------------------------------- #
# /chat — pending-confirmation decline + rollback
# --------------------------------------------------------------------------- #

def test_new_message_declines_pending_confirmation(auth_client, monkeypatch):
    # A write was awaiting confirmation; the user types a new message instead of
    # answering. That must be treated as declining the pending action so its
    # unanswered tool_call doesn't leave the history malformed.
    srv.conversations[SID] = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "send it"},
        {"role": "assistant", "content": "", "tool_calls": [EMAIL_CALL]},
    ]
    srv.pending_confirmations[SID] = EMAIL_CALL

    resolved = {}

    def fake_resolve(messages, call, approved, dispatch, logger=None):
        resolved["approved"] = approved
        messages.append({"role": "tool", "content": "declined"})

    def fake_advance(messages, tools, dispatch, confirm_before=frozenset(), logger=None,
                     should_cancel=None):
        return {"type": "final", "text": "ok, cancelled"}

    monkeypatch.setattr(srv, "resolve", fake_resolve)
    monkeypatch.setattr(srv, "advance", fake_advance)

    resp = auth_client.post("/chat", json={"message": "never mind"})
    assert resp.status_code == 200
    assert resp.get_json() == {"type": "final", "text": "ok, cancelled"}
    assert resolved["approved"] is False  # declined, not executed
    assert SID not in srv.pending_confirmations


def test_chat_rolls_back_history_when_advance_raises(auth_client, monkeypatch):
    def boom(messages, tools, dispatch, confirm_before=frozenset(), logger=None,
             should_cancel=None):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(srv, "advance", boom)

    resp = auth_client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 500
    # The failed turn's user message is rolled back; only the seeded system
    # prompt remains, so the next turn starts from a clean, valid history.
    history = srv.conversations[SID]
    assert len(history) == 1
    assert history[0]["role"] == "system"


def test_chat_confirm_keeps_resolved_result_on_failed_continuation(auth_client, monkeypatch):
    srv.conversations[SID] = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "send it"},
        {"role": "assistant", "content": "", "tool_calls": [EMAIL_CALL]},
    ]
    srv.pending_confirmations[SID] = EMAIL_CALL

    def fake_resolve(messages, call, approved, dispatch, logger=None):
        messages.append({"role": "tool", "content": "sent"})

    def boom(messages, tools, dispatch, confirm_before=frozenset(), logger=None,
             should_cancel=None):
        raise RuntimeError("continuation exploded")

    monkeypatch.setattr(srv, "resolve", fake_resolve)
    monkeypatch.setattr(srv, "advance", boom)

    resp = auth_client.post("/chat/confirm", json={"approved": True})
    assert resp.status_code == 500
    # The rollback must not strip the resolved tool result — that would orphan
    # the approved tool_call. It stays; only the failed continuation is removed.
    history = srv.conversations[SID]
    assert history[-1] == {"role": "tool", "content": "sent"}


def test_chat_cancelled_returns_stopped_and_rolls_back(auth_client, monkeypatch):
    # A cancel raised mid-turn is reported as stopped (200), and the partial
    # turn is rolled back so history stays clean for the next message.
    def cancelled(messages, tools, dispatch, confirm_before=frozenset(), logger=None,
                  should_cancel=None):
        raise srv.TurnCancelled()

    monkeypatch.setattr(srv, "advance", cancelled)

    resp = auth_client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    assert resp.get_json() == {"type": "cancelled"}
    history = srv.conversations[SID]
    assert len(history) == 1 and history[0]["role"] == "system"
    assert SID not in srv.cancel_events  # the turn's event was cleaned up


def test_chat_cancel_sets_active_turns_event(auth_client):
    # /chat/cancel signals the event the running turn is watching.
    event = srv.cancel_events[SID] = threading.Event()
    resp = auth_client.post("/chat/cancel")
    assert resp.status_code == 200
    assert resp.get_json()["cancelling"] is True
    assert event.is_set()


def test_chat_cancel_with_no_active_turn_is_noop(auth_client):
    resp = auth_client.post("/chat/cancel")
    assert resp.status_code == 200
    assert resp.get_json()["cancelling"] is False


def test_chat_confirm_without_pending_is_400(auth_client):
    resp = auth_client.post("/chat/confirm", json={"approved": True})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "no pending action"


# --------------------------------------------------------------------------- #
# /api/memories
# --------------------------------------------------------------------------- #

def test_api_memories_splits_scope_and_sorts_archival_by_access_count(auth_client, tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "_STORE_PATH", tmp_path / "wren_memory.json")
    memory.pin("Craig prefers metric units", category="preference")
    memory.remember("Crows can recognize human faces", category="trivia")
    memory.remember("Owls can rotate their heads 270 degrees", category="trivia")
    memory.recall(query="owls")  # bumps owls' access_count to 1; crows stays at 0

    resp = auth_client.get("/api/memories")
    assert resp.status_code == 200
    data = resp.get_json()
    assert [m["text"] for m in data["active"]] == ["Craig prefers metric units"]
    assert [m["text"] for m in data["archival"]] == [
        "Owls can rotate their heads 270 degrees",
        "Crows can recognize human faces",
    ]


# --------------------------------------------------------------------------- #
# /map + /api/system_map
# --------------------------------------------------------------------------- #

def test_map_page_shows_login_when_unauthenticated(client):
    resp = client.get("/map")
    assert resp.status_code == 200
    assert b"Access token" in resp.data


def test_map_page_serves_map_when_authenticated(auth_client):
    resp = auth_client.get("/map")
    assert resp.status_code == 200
    assert b"system map" in resp.data


def test_api_system_map_shape(auth_client, tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "_STORE_PATH", tmp_path / "wren_memory.json")
    memory.remember("Crows can recognize human faces", category="trivia")
    monkeypatch.setenv("WREN_SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path / "no-vault"))

    resp = auth_client.get("/api/system_map")
    assert resp.status_code == 200
    data = resp.get_json()
    assert set(data) == {"identity", "services", "routines", "memory", "skills"}
    assert data["identity"]["name"] == "Wren"
    # Every registered chat tool lands in exactly one service group.
    grouped = [t["name"] for s in data["services"] for t in s["tools"]]
    registered = [t["function"]["name"] for t in srv.TOOLS]
    assert sorted(grouped) == sorted(registered)
    # The unmounted vault degrades to an empty wiki band, never an error.
    assert data["memory"]["wiki_pages"] == []
    assert [m["text"] for m in data["memory"]["entries"]] == ["Crows can recognize human faces"]
    assert data["skills"] == []
    for rt in data["routines"]:
        assert set(rt) == {"key", "display_name", "human_schedule", "next_run", "last_run", "uses"}


# --------------------------------------------------------------------------- #
# Background-approval endpoint (token-authed, NOT session-authed)
# --------------------------------------------------------------------------- #

def _seed_awaiting_job(monkeypatch, tmp_path):
    from agent.tools import background
    monkeypatch.setattr(background, "_STORE_PATH", tmp_path / "bg.json")
    jid = background.run_in_background("x")["id"]
    background.save_awaiting(jid, [], {"function": {"name": "send_email", "arguments": {}}})
    return background, jid


def _capture_ack(monkeypatch):
    """Stub the server's notify() so the endpoint's ack push never hits the real
    ntfy server; return the list of captured ack titles."""
    acks = []
    monkeypatch.setattr(srv, "notify",
                        lambda title=None, message=None, **k: acks.append(title) or {"ok": True})
    return acks


def test_bg_resolve_rejects_bad_token(client, monkeypatch):
    acks = _capture_ack(monkeypatch)
    resp = client.post("/api/bg/resolve?token=garbage")
    assert resp.status_code == 403
    assert acks == []  # no ack on a rejected token


def test_bg_resolve_applies_valid_token_and_acks(client, monkeypatch, tmp_path):
    acks = _capture_ack(monkeypatch)
    background, jid = _seed_awaiting_job(monkeypatch, tmp_path)
    token = background.make_approval_token(jid, "approve")
    resp = client.post(f"/api/bg/resolve?token={token}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True and body["decision"] == "approve"
    assert background.get_job_result(jid)["status"] == "approved"
    assert acks == ["Approved"]  # exactly one ack, on the real transition


def test_bg_resolve_token_is_single_use_and_acks_once(client, monkeypatch, tmp_path):
    acks = _capture_ack(monkeypatch)
    background, jid = _seed_awaiting_job(monkeypatch, tmp_path)
    token = background.make_approval_token(jid, "approve")
    assert client.post(f"/api/bg/resolve?token={token}").get_json()["ok"] is True
    # Replay: the job is no longer awaiting, so the same token is inert.
    assert client.post(f"/api/bg/resolve?token={token}").get_json()["ok"] is False
    assert acks == ["Approved"]  # only the first tap acks, not the replay


def test_bg_resolve_deny_acks_denied(client, monkeypatch, tmp_path):
    acks = _capture_ack(monkeypatch)
    background, jid = _seed_awaiting_job(monkeypatch, tmp_path)
    token = background.make_approval_token(jid, "deny")
    assert client.post(f"/api/bg/resolve?token={token}").get_json()["ok"] is True
    assert background.get_job_result(jid)["status"] == "denied"
    assert acks == ["Denied"]
