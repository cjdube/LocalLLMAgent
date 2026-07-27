"""Tests for chat.server's stateful, security-relevant HTTP paths.

Covers auth gating on every endpoint, the login throttle's 429, the
email-body-preview surfaced in confirmations, and the two subtle flows that
keep conversation history well-formed: declining a pending write when the user
sends a new message instead of answering, and rolling back a failed turn.

The model and network are never touched — chat.server.advance / resolve are
monkeypatched. Importing chat.server runs its module-level secret check, so the
two required secrets are stubbed into the environment before the import.
"""

import logging
import os
import threading
import time

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
    srv.pending_backends.clear()
    srv.loaded_groups.clear()
    srv.cancel_events.clear()
    srv._session_last_active.clear()
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
    ("post", "/chat/escalate", {}),
    ("post", "/chat/cancel", {}),
    ("post", "/chat/new", {}),
    ("get", "/api/schedules", {}),
    ("get", "/api/runs/morning_brief", {}),
    ("get", "/api/runs/morning_brief/someid", {}),
    ("get", "/api/capabilities", {}),
    ("get", "/api/run_stats", {}),
    ("get", "/api/health/ntfy", {}),
    ("post", "/api/run/morning_brief", {}),
    ("get", "/api/run/morning_brief/status", {}),
    ("get", "/api/memories", {}),
    ("get", "/api/system_map", {}),
    ("get", "/api/opportunities", {}),
    ("get", "/api/starred", {}),
    ("post", "/api/opportunities/abc/status", {"json": {"status": "interested"}}),
    ("post", "/api/opportunities/watchlist", {"json": {"company": "X"}}),
    ("delete", "/api/opportunities/watchlist/abc", {}),
    ("post", "/api/opportunities/abc/research", {}),
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
# /api/starred — live repo list merged with cached blurbs
# --------------------------------------------------------------------------- #

def test_api_starred_merges_cached_blurb_and_falls_back_to_description(auth_client, monkeypatch):
    from agent.store import atomic_write_json
    monkeypatch.setattr(srv, "fetch_starred_repos", lambda: {"repos": [
        {"full_name": "a/one", "description": "desc one", "language": "Rust"},
        {"full_name": "b/two", "description": "desc two", "language": "Go"},
    ]})
    # a/one is cached; b/two is not, so it falls back to its GitHub description.
    atomic_write_json(srv.starred_blurbs.BLURBS_PATH,
                      {"a/one": {"blurb": "cached blurb", "generated_at": "x"}})

    resp = auth_client.get("/api/starred")
    assert resp.status_code == 200
    repos = {r["full_name"]: r["blurb"] for r in resp.get_json()["repos"]}
    assert repos == {"a/one": "cached blurb", "b/two": "desc two"}


def test_api_starred_merges_cached_release_with_new_badge(auth_client, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from agent.store import atomic_write_json
    monkeypatch.setattr(srv, "fetch_starred_repos", lambda: {"repos": [
        {"full_name": "a/recent", "description": "d", "language": "Rust"},
        {"full_name": "b/old", "description": "d", "language": "Go"},
        {"full_name": "c/none", "description": "d", "language": "C"},
    ]})
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    atomic_write_json(srv.starred_releases.RELEASES_PATH, {
        "a/recent": {"tag": "v1.3", "name": "1.3", "published_at": recent, "html_url": "u"},
        "b/old": {"tag": "v0.9", "name": "0.9", "published_at": old, "html_url": "u"},
    })

    resp = auth_client.get("/api/starred")
    assert resp.status_code == 200
    repos = {r["full_name"]: r for r in resp.get_json()["repos"]}
    assert repos["a/recent"]["latest_release"]["tag"] == "v1.3"
    assert repos["a/recent"]["release_is_new"] is True
    assert repos["b/old"]["release_is_new"] is False        # released, but outside the window
    assert repos["c/none"]["latest_release"] is None         # no cached release
    assert repos["c/none"]["release_is_new"] is False


def test_api_starred_merges_installed_version_and_update_flag(auth_client, monkeypatch):
    from agent.store import atomic_write_json
    monkeypatch.setattr(srv, "fetch_starred_repos", lambda: {"repos": [
        {"full_name": "a/outdated", "description": "d", "language": "Rust"},
        {"full_name": "b/current", "description": "d", "language": "Go"},
        {"full_name": "c/untracked", "description": "d", "language": "C"},
        {"full_name": "d/broken", "description": "d", "language": "C"},
    ]})
    atomic_write_json(srv.starred_releases.RELEASES_PATH, {
        "a/outdated": {"tag": "v1.3.0", "name": "1.3.0", "published_at": None, "html_url": "u"},
        "b/current": {"tag": "v2.0.0", "name": "2.0.0", "published_at": None, "html_url": "u"},
    })
    atomic_write_json(srv.starred_installed.INSTALLED_PATH, {
        "a/outdated": {"version": "1.1.0", "source": "cmd", "error": None},
        "b/current": {"version": "v2.0.0", "source": "manual", "error": None},
        "d/broken": {"version": None, "source": "cmd", "error": "FileNotFoundError: rtk"},
    })

    resp = auth_client.get("/api/starred")
    assert resp.status_code == 200
    repos = {r["full_name"]: r for r in resp.get_json()["repos"]}
    assert repos["a/outdated"]["installed_version"] == "1.1.0"
    assert repos["a/outdated"]["update_available"] is True       # 1.1.0 < v1.3.0
    assert repos["b/current"]["update_available"] is False        # v2.0.0 == v2.0.0
    assert repos["c/untracked"]["installed_version"] is None      # not tracked
    assert repos["c/untracked"]["update_available"] is None
    assert repos["d/broken"]["installed_version"] is None
    assert repos["d/broken"]["installed_error"] == "FileNotFoundError: rtk"


def test_api_starred_passes_fetch_error_through(auth_client, monkeypatch):
    monkeypatch.setattr(srv, "fetch_starred_repos", lambda: {"error": "rate limited"})
    resp = auth_client.get("/api/starred")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["error"] == "rate limited"
    assert body["repos"] == []


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


def test_login_throttle_ignores_spoofed_xff_from_direct_clients(client):
    # X-Forwarded-For is only honored when the peer is loopback (the
    # `tailscale serve` shape). A direct client rotating the header per
    # attempt must still be keyed by its real address — and locked out.
    direct = {"REMOTE_ADDR": "198.51.100.7"}
    for i in range(srv.LoginThrottle.MAX_FAILURES):
        resp = client.post("/login", data={"token": "wrong"},
                           headers={"X-Forwarded-For": f"10.0.0.{i}"},
                           environ_base=direct)
        assert resp.status_code == 401
    resp = client.post("/login", data={"token": "wrong"},
                       headers={"X-Forwarded-For": "10.0.0.99"},
                       environ_base=direct)
    assert resp.status_code == 429


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


def test_chat_rebuilds_system_message_every_turn(auth_client, monkeypatch):
    """A fact pinned (or skill saved) mid-session must reach the model on the
    very next turn: /chat rebuilds history[0] from _system_message_content()
    on every message, not just when the session starts."""
    srv.conversations[SID] = [{"role": "system", "content": "stale prompt"}, *_turn(1)]
    monkeypatch.setattr(srv, "_system_message_content", lambda: "fresh prompt")

    def fake_advance(messages, tools, dispatch, confirm_before=frozenset(), logger=None,
                     should_cancel=None):
        return {"type": "final", "text": "done"}

    monkeypatch.setattr(srv, "advance", fake_advance)
    resp = auth_client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200

    history = srv.conversations[SID]
    assert history[0] == {"role": "system", "content": "fresh prompt"}
    # Still exactly one system message — the rebuild replaces, never stacks.
    assert [m["role"] for m in history].count("system") == 1


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


def test_chat_logs_turn_start_before_advancing(auth_client, monkeypatch):
    """The turn-start line must be written *before* advance() runs. Every other
    per-turn line (the access log, ollama_chat) lands only once the turn
    finishes, so without this one a request that hangs mid-turn and a request
    that never arrived are both simply absent from the log — which is exactly
    what made a dropped connection undiagnosable. srv.logger sets
    propagate=False, so caplog can't see it; record off the logger itself.
    """
    records = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())
    srv.logger.addHandler(handler)

    logged_by_the_time_advance_ran = []

    def fake_advance(messages, tools, dispatch, confirm_before=frozenset(), logger=None,
                     should_cancel=None):
        logged_by_the_time_advance_ran.extend(records)
        return {"type": "final", "text": "done"}

    monkeypatch.setattr(srv, "advance", fake_advance)
    try:
        resp = auth_client.post("/chat", json={"message": "hi"})
    finally:
        srv.logger.removeHandler(handler)

    assert resp.status_code == 200
    assert any("chat turn start" in m for m in logged_by_the_time_advance_ran)


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


def test_chat_new_cancels_a_running_turn(auth_client):
    # Starting a fresh chat while a turn is still running must stop it, or the
    # orphan keeps the sid's turn slot (409ing the next /chat) and can still
    # park a confirmation on the now-empty session.
    event = srv.cancel_events[SID] = threading.Event()
    resp = auth_client.post("/chat/new")
    assert resp.status_code == 200
    assert event.is_set()


def test_chat_new_with_no_running_turn_still_clears(auth_client):
    srv.conversations[SID] = [{"role": "user", "content": "hi"}]
    resp = auth_client.post("/chat/new")
    assert resp.status_code == 200
    assert SID not in srv.conversations


def test_chat_confirm_without_pending_is_400(auth_client):
    resp = auth_client.post("/chat/confirm", json={"approved": True})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "no pending action"
    # The 400 path must release the turn slot it briefly claimed, or the
    # session would be stuck answering 409 forever.
    assert SID not in srv.cancel_events


# --------------------------------------------------------------------------- #
# One turn per session — concurrent requests get 409, not interleaved history
# --------------------------------------------------------------------------- #

def test_second_chat_while_turn_running_is_409(auth_client):
    srv.cancel_events[SID] = threading.Event()  # a turn is mid-flight
    resp = auth_client.post("/chat", json={"message": "impatient double-send"})
    assert resp.status_code == 409
    assert "already running" in resp.get_json()["error"]
    # The running turn's cancel Event was not clobbered.
    assert SID in srv.cancel_events


def test_chat_confirm_while_turn_running_is_409(auth_client):
    srv.cancel_events[SID] = threading.Event()
    srv.pending_confirmations[SID] = EMAIL_CALL
    resp = auth_client.post("/chat/confirm", json={"approved": True})
    assert resp.status_code == 409
    # The pending confirmation is untouched — answerable once the turn ends.
    assert srv.pending_confirmations[SID] == EMAIL_CALL


# --------------------------------------------------------------------------- #
# Idle-session eviction
# --------------------------------------------------------------------------- #

def test_idle_sessions_evicted_on_next_chat(auth_client, monkeypatch):
    srv.conversations["stale-sid"] = [{"role": "system", "content": "s"}]
    srv.pending_confirmations["stale-sid"] = EMAIL_CALL
    srv._session_last_active["stale-sid"] = time.time() - srv.SESSION_IDLE_EVICT_S - 1
    srv.conversations["fresh-sid"] = [{"role": "system", "content": "s"}]
    srv._session_last_active["fresh-sid"] = time.time()

    monkeypatch.setattr(srv, "advance",
                        lambda *a, **k: {"type": "final", "text": "hi"})
    resp = auth_client.post("/chat", json={"message": "hello"})
    assert resp.status_code == 200

    assert "stale-sid" not in srv.conversations
    assert "stale-sid" not in srv.pending_confirmations
    assert "fresh-sid" in srv.conversations
    assert SID in srv.conversations  # the active session obviously survives


# --------------------------------------------------------------------------- #
# /api/memories
# --------------------------------------------------------------------------- #

def test_api_memories_splits_scope_and_sorts_archival_by_access_count(auth_client, tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "_STORE_PATH", tmp_path / "wren_memory.json")
    memory.pin("I prefer metric units", category="preference")
    memory.remember("Crows can recognize human faces", category="trivia")
    memory.remember("Owls can rotate their heads 270 degrees", category="trivia")
    memory.recall(query="owls")  # bumps owls' access_count to 1; crows stays at 0

    resp = auth_client.get("/api/memories")
    assert resp.status_code == 200
    data = resp.get_json()
    assert [m["text"] for m in data["active"]] == ["I prefer metric units"]
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
    # The missing vault degrades to an empty wiki band, never an error.
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


# --------------------------------------------------------------------------- #
# /api/opportunities — the /opportunities page's triage endpoints
# --------------------------------------------------------------------------- #

@pytest.fixture
def opp_store(tmp_path, monkeypatch):
    """Isolate the opportunities store and seed one triageable item."""
    from agent.tools import opportunities as opp
    monkeypatch.setattr(opp, "_STORE_PATH", tmp_path / "opportunities.json")
    item_id = opp.insert_new_items([{
        "id": "hn:1", "source": "hn", "signal": "hiring", "company": "TinyCo",
        "title": "Head of Product", "url": "https://example.com",
        "posted_at": None,
    }])[0]["id"]
    return opp, item_id


def test_api_opportunities_lists_items_and_watchlist(auth_client, opp_store):
    opp, _ = opp_store
    opp.watch_company("Acme", "greenhouse", "acme")
    data = auth_client.get("/api/opportunities").get_json()
    assert [i["id"] for i in data["items"]] == ["hn:1"]
    assert [w["slug"] for w in data["watchlist"]] == ["acme"]


def test_api_opportunity_status_triages_via_the_shared_store(auth_client, opp_store):
    opp, item_id = opp_store
    resp = auth_client.post(f"/api/opportunities/{item_id}/status",
                            json={"status": "interested"})
    assert resp.status_code == 200
    # Same store the chat tool reads — the page and chat can't drift apart.
    assert opp.list_opportunities(status="interested")["count"] == 1


def test_api_opportunity_status_rejects_bad_input(auth_client, opp_store):
    _, item_id = opp_store
    assert auth_client.post(f"/api/opportunities/{item_id}/status",
                            json={"status": "digested"}).status_code == 400
    assert auth_client.post("/api/opportunities/missing/status",
                            json={"status": "dismissed"}).status_code == 400
    assert auth_client.post(f"/api/opportunities/{item_id}/status",
                            json=None).status_code == 400


def test_api_watchlist_add_and_remove(auth_client, opp_store):
    opp, _ = opp_store
    resp = auth_client.post("/api/opportunities/watchlist",
                            json={"company": "Acme", "ats": "lever", "slug": "acme"})
    assert resp.status_code == 200
    wid = resp.get_json()["id"]
    assert [w["id"] for w in opp.get_watchlist()] == [wid]
    assert auth_client.post("/api/opportunities/watchlist",
                            json={"company": "Bad", "ats": "workday", "slug": "x"}
                            ).status_code == 400
    assert auth_client.delete(f"/api/opportunities/watchlist/{wid}").status_code == 200
    assert opp.get_watchlist() == []
    assert auth_client.delete("/api/opportunities/watchlist/nope").status_code == 400


# --------------------------------------------------------------------------- #
# Research triggers (the pipeline itself is covered in test_research.py)
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def research_spy(monkeypatch):
    """Replace the thread-spawning runner with a recorder that also writes the
    pending marker, mirroring the real _start_research's synchronous half.

    Autouse for the whole file: any status POST can trigger _start_research,
    and the real one spawns a daemon thread running the real pipeline. A
    thread that outlives its test races monkeypatch teardown — it once loaded
    a tmp-store's fixture data and saved it over the production store when
    the path monkeypatch was undone mid-write. No test may spawn it."""
    from agent.tools import opportunities as opp
    from chat import routes_opportunities
    started = []

    def fake_start(item):
        started.append(item["id"])
        opp.set_research(item["id"], {"status": "pending", "summary": None})

    # _start_research now lives on the opportunities blueprint module, where the
    # triage routes call it — stub it there so no real research thread spawns.
    monkeypatch.setattr(routes_opportunities, "_start_research", fake_start)
    return started


def test_research_endpoint_starts_a_run(auth_client, opp_store, research_spy):
    _, item_id = opp_store
    resp = auth_client.post(f"/api/opportunities/{item_id}/research")
    assert resp.status_code == 202
    assert research_spy == [item_id]
    # Already pending: acknowledged, not restarted.
    resp = auth_client.post(f"/api/opportunities/{item_id}/research")
    assert resp.status_code == 200 and "already" in resp.get_json()["note"]
    assert research_spy == [item_id]
    assert auth_client.post("/api/opportunities/missing/research").status_code == 400


def test_marking_interested_auto_researches_once(auth_client, opp_store, research_spy):
    opp, item_id = opp_store
    assert auth_client.post(f"/api/opportunities/{item_id}/status",
                            json={"status": "interested"}).status_code == 200
    assert research_spy == [item_id]
    # Re-marking interested later (research already present) doesn't re-run.
    opp.set_research(item_id, {"status": "done", "summary": "brief"})
    auth_client.post(f"/api/opportunities/{item_id}/status", json={"status": "interested"})
    assert research_spy == [item_id]
    # Dismissing never triggers research.
    auth_client.post(f"/api/opportunities/{item_id}/status", json={"status": "dismissed"})
    assert research_spy == [item_id]


# --------------------------------------------------------------------------- #
# Lazy tool loading: chat sends only the core plus the session's activated
# groups. Groups are pre-loaded by keyword and, as a fallback, by the model
# calling load_tools (which extends the live turn's tools and persists the
# group on the session).
# --------------------------------------------------------------------------- #

def test_chat_sends_core_only_when_no_group_cued(auth_client, monkeypatch):
    seen = {}

    def fake_advance(messages, tools, dispatch, confirm_before=frozenset(), logger=None,
                     should_cancel=None):
        seen["names"] = {t["function"]["name"] for t in tools}
        seen["dispatch_has_load"] = "load_tools" in dispatch
        return {"type": "final", "text": "sunny"}

    monkeypatch.setattr(srv, "advance", fake_advance)
    resp = auth_client.post("/chat", json={"message": "what's the weather?"})
    assert resp.status_code == 200
    # Core tool present, deferred group tool absent, meta-tool always offered.
    assert "fetch_weather" in seen["names"]
    assert "list_opportunities" not in seen["names"]
    assert "load_tools" in seen["names"]
    assert seen["dispatch_has_load"]
    assert SID not in srv.loaded_groups or not srv.loaded_groups[SID]


def test_chat_keyword_preloads_a_group(auth_client, monkeypatch):
    seen = {}

    def fake_advance(messages, tools, dispatch, confirm_before=frozenset(), logger=None,
                     should_cancel=None):
        seen["names"] = {t["function"]["name"] for t in tools}
        return {"type": "final", "text": "here's your watchlist"}

    monkeypatch.setattr(srv, "advance", fake_advance)
    resp = auth_client.post("/chat", json={"message": "what's on my opportunities watchlist?"})
    assert resp.status_code == 200
    # The 'opportunities' group was attached before the model ran — no load hop.
    assert "list_opportunities" in seen["names"]
    assert srv.loaded_groups[SID] == {"opportunities"}


def test_make_load_tools_extends_live_list_and_persists(monkeypatch):
    srv.loaded_groups.pop(SID, None)
    tools = list(srv.tools_for(set()))
    before = {t["function"]["name"] for t in tools}
    load = srv._make_load_tools(SID, tools)

    result = load(group="wiki")
    after = {t["function"]["name"] for t in tools}
    # The live list grew in place with the wiki group's tools...
    assert "read_wiki_index" in after and "read_wiki_index" not in before
    assert "read_wiki_index" in result["now_available"]
    # ...and the group is recorded on the session for later turns / continuation.
    assert srv.loaded_groups[SID] == {"wiki"}
    # Idempotent: loading again adds nothing new.
    assert load(group="wiki")["now_available"] == []
    # Unknown group is a soft error listing the choices, not a crash.
    err = load(group="nope")
    assert "error" in err and "wiki" in err["available"]
    srv.loaded_groups.pop(SID, None)


def test_loaded_groups_persist_across_turns_and_clear_on_new(auth_client, monkeypatch):
    def fake_advance(messages, tools, dispatch, confirm_before=frozenset(), logger=None,
                     should_cancel=None):
        return {"type": "final", "text": "ok"}

    monkeypatch.setattr(srv, "advance", fake_advance)
    auth_client.post("/chat", json={"message": "anything about my strava runs?"})
    assert "activity" in srv.loaded_groups[SID]
    # A later plain turn keeps the group loaded (persists across turns).
    auth_client.post("/chat", json={"message": "thanks"})
    assert "activity" in srv.loaded_groups[SID]
    # Starting a new session drops it.
    auth_client.post("/chat/new")
    assert SID not in srv.loaded_groups


# --------------------------------------------------------------------------- #
# Startup context-budget check
# --------------------------------------------------------------------------- #

def _budget(monkeypatch, history, num_ctx, iterations=10, result_chars=8000):
    monkeypatch.setattr(srv, "MAX_HISTORY_CHARS", history)
    monkeypatch.setattr(srv, "MAX_TOOL_ITERATIONS", iterations)
    monkeypatch.setattr(srv, "MAX_TOOL_RESULT_CHARS", result_chars)
    monkeypatch.setenv("OLLAMA_NUM_CTX", str(num_ctx))
    return srv._context_budget_warning()


def test_shipped_config_is_within_budget_and_stays_quiet(monkeypatch):
    # The real config/.env values. A check that fires on the working setup would
    # get muted, so pin that it doesn't.
    assert _budget(monkeypatch, history=48000, num_ctx=32768) is None


def test_raising_history_without_num_ctx_warns(monkeypatch):
    # The exact footgun: the two knobs must move together.
    warning = _budget(monkeypatch, history=96000, num_ctx=32768)
    assert warning is not None
    assert "num_ctx" in warning and "96,000" in warning


def test_warning_names_the_knobs_to_change(monkeypatch):
    warning = _budget(monkeypatch, history=96000, num_ctx=32768)
    for knob in ("OLLAMA_NUM_CTX", "WREN_CHAT_MAX_HISTORY_CHARS",
                 "OLLAMA_MAX_TOOL_RESULT_CHARS"):
        assert knob in warning


def test_budget_counts_tool_results_not_just_history(monkeypatch):
    # History alone fits; it's the in-turn tool results that blow the ceiling —
    # the whole point of the check.
    assert _budget(monkeypatch, history=16000, num_ctx=8192, iterations=10,
                   result_chars=8000) is not None
    assert _budget(monkeypatch, history=16000, num_ctx=8192, iterations=1,
                   result_chars=100) is None


def test_main_logs_and_pushes_when_over_budget(monkeypatch, caplog):
    # The wiring, not the arithmetic: an over-budget config must reach both
    # surfaces. The log alone is invisible (the dashboard skips daemon logs),
    # so the push is the part the user actually sees.
    pushes = []
    monkeypatch.setattr(srv, "notify", lambda **kw: pushes.append(kw))
    monkeypatch.setattr(srv.app, "run", lambda **kw: None)
    monkeypatch.setattr(srv, "_context_budget_warning", lambda: "OVER BUDGET")

    with caplog.at_level(logging.WARNING, logger="wren"):
        srv.main()

    assert pushes and "OVER BUDGET" in pushes[0]["message"]
    assert any("OVER BUDGET" in r.message for r in caplog.records)


def test_main_stays_silent_when_within_budget(monkeypatch):
    pushes = []
    monkeypatch.setattr(srv, "notify", lambda **kw: pushes.append(kw))
    monkeypatch.setattr(srv.app, "run", lambda **kw: None)
    monkeypatch.setattr(srv, "_context_budget_warning", lambda: None)

    srv.main()
    assert pushes == []


# --------------------------------------------------------------------------- #
# Push-channel health (the dashboard pill)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("health", [
    {"state": "ok", "error": None},
    {"state": "down", "error": "ntfy unreachable: refused"},
    {"state": "off", "error": None},
])
def test_health_ntfy_passes_probe_result_through(auth_client, monkeypatch, health):
    # /api/health/ntfy now lives on the dashboard blueprint; patch it there.
    from chat import routes_dashboard
    monkeypatch.setattr(routes_dashboard, "ntfy_health", lambda: health)
    resp = auth_client.get("/api/health/ntfy")
    assert resp.status_code == 200
    assert resp.get_json() == health


# --------------------------------------------------------------------------- #
# /api/run_stats (the dashboard's duration charts)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("query,expected", [
    ("", 30),               # default window
    ("?days=7", 7),
    ("?days=0", 30),        # falsy -> default, not a zero-day window
    ("?days=-5", 1),        # clamped up
    ("?days=9999", 365),    # clamped down
    ("?days=soon", 30),     # unparseable -> default
])
def test_run_stats_clamps_the_window_instead_of_rejecting_it(
        auth_client, monkeypatch, query, expected):
    # ?days= is a chart window: a nonsense value should narrow the chart, not
    # 400 the page that asked for it.
    seen = {}
    from chat import routes_dashboard

    def fake_run_stats(days):
        seen["days"] = days
        return {"days": days, "tasks": []}

    monkeypatch.setattr(routes_dashboard, "run_stats", fake_run_stats)
    resp = auth_client.get("/api/run_stats" + query)
    assert resp.status_code == 200
    assert seen["days"] == expected


# --------------------------------------------------------------------------- #
# /chat/escalate — manual "redo with the frontier model"
# --------------------------------------------------------------------------- #

from agent import escalations
from agent.store import load_json


def _escalation_rows():
    return load_json(escalations._STORE_PATH, {"escalations": []})["escalations"]


@pytest.fixture
def frontier_configured(monkeypatch):
    """A configured, credentialled frontier backend, so escalation_available()
    is true and the button/endpoint are live."""
    monkeypatch.setenv("WREN_ESCALATION_BACKEND", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")


def _seed_completed_turn():
    srv.conversations[SID] = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "summarize my week"},
        {"role": "assistant", "content": "weak local answer"},
    ]


def _frontier_advance(text="strong frontier answer"):
    """A fake advance that stands in for the frontier backend: appends a final
    assistant turn and returns it. Accepts `backend` — the escalation path passes
    it, unlike the local-turn doubles elsewhere in this file."""
    def fake(messages, tools, dispatch, confirm_before=frozenset(), logger=None,
             should_cancel=None, backend=None):
        fake.backend = backend
        messages.append({"role": "assistant", "content": text})
        return {"type": "final", "text": text}
    return fake


def test_escalate_requires_a_configured_backend(auth_client):
    # No WREN_ESCALATION_BACKEND set — the endpoint refuses rather than pretending.
    _seed_completed_turn()
    resp = auth_client.post("/chat/escalate")
    assert resp.status_code == 400
    assert "no frontier backend" in resp.get_json()["error"]
    assert SID not in srv.cancel_events  # the turn slot it briefly took is released


def test_escalate_reruns_last_turn_and_badges_it(auth_client, monkeypatch, frontier_configured):
    _seed_completed_turn()
    monkeypatch.setattr(srv, "advance", _frontier_advance())

    resp = auth_client.post("/chat/escalate")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["type"] == "final"
    assert body["text"] == "strong frontier answer"
    assert body["escalated"] is True
    assert "gemini" in body["model_label"]

    # The weak local reply was dropped; the committed history re-runs the same
    # user request and ends on the frontier answer.
    roles = [(m["role"], m.get("content")) for m in srv.conversations[SID]]
    assert roles == [("system", "s"), ("user", "summarize my week"),
                     ("assistant", "strong frontier answer")]


def test_escalate_logs_the_paired_record(auth_client, monkeypatch, frontier_configured):
    _seed_completed_turn()
    monkeypatch.setattr(srv, "advance", _frontier_advance())

    auth_client.post("/chat/escalate")
    rows = _escalation_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["request"] == "summarize my week"
    assert row["local_reply"] == "weak local answer"  # the paired half of the dataset
    assert row["backend"] == "gemini"
    assert row["outcome"] == "ok"
    assert row["prompt_tokens"] > 0


def test_escalate_runs_on_the_frontier_backend(auth_client, monkeypatch, frontier_configured):
    _seed_completed_turn()
    fake = _frontier_advance()
    monkeypatch.setattr(srv, "advance", fake)
    auth_client.post("/chat/escalate")
    assert fake.backend == "gemini"  # advance() was told to use the frontier backend


def test_escalate_with_nothing_to_redo_is_400(auth_client, frontier_configured):
    srv.conversations[SID] = [{"role": "system", "content": "s"}]  # no user turn yet
    resp = auth_client.post("/chat/escalate")
    assert resp.status_code == 400
    assert "nothing to redo" in resp.get_json()["error"]
    assert SID not in srv.cancel_events  # slot released, no stuck session


def test_escalate_failure_keeps_the_local_answer_and_logs_the_error(
        auth_client, monkeypatch, frontier_configured):
    _seed_completed_turn()
    before = list(srv.conversations[SID])

    def boom(messages, tools, dispatch, confirm_before=frozenset(), logger=None,
             should_cancel=None, backend=None):
        raise RuntimeError("frontier unreachable")

    monkeypatch.setattr(srv, "advance", boom)
    resp = auth_client.post("/chat/escalate")
    assert resp.status_code == 502
    assert "unchanged" in resp.get_json()["error"]
    # The local answer survives — the failed frontier turn ran on a copy.
    assert srv.conversations[SID] == before
    # ...and the failure is recorded for the audit trail.
    rows = _escalation_rows()
    assert rows[-1]["outcome"].startswith("error:")
    assert rows[-1]["local_reply"] == "weak local answer"


def test_escalate_while_turn_running_is_409(auth_client, frontier_configured):
    _seed_completed_turn()
    srv.cancel_events[SID] = threading.Event()  # a turn is mid-flight
    resp = auth_client.post("/chat/escalate")
    assert resp.status_code == 409
    assert SID in srv.cancel_events  # the running turn's event was not clobbered


def test_escalate_paused_write_continues_on_the_frontier(auth_client, monkeypatch, frontier_configured):
    # A frontier turn that hits a write gate parks the confirmation AND remembers
    # its backend, so /chat/confirm resumes on the frontier model, not local.
    _seed_completed_turn()

    def wants_to_write(messages, tools, dispatch, confirm_before=frozenset(),
                       logger=None, should_cancel=None, backend=None):
        return {"type": "confirm", "call": EMAIL_CALL}

    monkeypatch.setattr(srv, "advance", wants_to_write)
    resp = auth_client.post("/chat/escalate")
    assert resp.status_code == 200
    assert resp.get_json()["type"] == "confirm"
    assert srv.pending_confirmations[SID] == EMAIL_CALL
    assert srv.pending_backends[SID] == "gemini"


def test_local_final_advertises_escalation_when_configured(auth_client, monkeypatch, frontier_configured):
    monkeypatch.setattr(srv, "advance",
                        lambda *a, **k: {"type": "final", "text": "local answer"})
    resp = auth_client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    # The dock uses escalate_to to decide whether to offer the redo button.
    assert "gemini" in resp.get_json()["escalate_to"]


def test_local_final_omits_escalation_when_not_configured(auth_client, monkeypatch):
    # No frontier backend configured → no redo affordance advertised.
    monkeypatch.setattr(srv, "advance",
                        lambda *a, **k: {"type": "final", "text": "local answer"})
    resp = auth_client.post("/chat", json={"message": "hi"})
    assert "escalate_to" not in resp.get_json()
