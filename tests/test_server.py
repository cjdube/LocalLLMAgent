"""Tests for chat.server's stateful, security-relevant HTTP paths.

Covers auth gating on every endpoint, the login throttle's 429, the
email-body-preview surfaced in confirmations, and the two subtle flows that
keep conversation history well-formed: declining a pending write when the user
sends a new message instead of answering, and rolling back a failed turn.

The model and network are never touched — chat.server.advance / resolve are
monkeypatched. Importing chat.server runs its module-level secret check, so the
two required secrets are stubbed into the environment before the import.
"""

import json
import logging
import os
import pathlib
import threading
import time

os.environ.setdefault("WREN_CHAT_TOKEN", "test-token")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import pytest

from agent import toolset
from agent.tools import memory
from chat import server as srv


@pytest.fixture(autouse=True)
def compaction_model(monkeypatch):
    """Stub the compaction summarizer's model call for the whole file.

    Autouse because any /chat that goes over the history budget now calls it,
    which would otherwise be a real Ollama request from an unrelated test. The
    compaction tests below override the return value; everything else just needs
    it not to reach the network."""
    calls = []

    def fake_complete_text(system_prompt, user_prompt, **kwargs):
        calls.append({"system": system_prompt, "prompt": user_prompt, **kwargs})
        return "- a summary"

    monkeypatch.setattr(srv, "complete_text", fake_complete_text)
    return calls


@pytest.fixture(autouse=True)
def busy_probe(monkeypatch):
    """Report the local model's slot as free for the whole file.

    Autouse for the same reason as compaction_model above: /chat now asks
    Ollama whether its one request slot is free before committing a turn to it,
    which would be a real network call from every unrelated test here. The
    busy-path tests below override the return value.

    Returns the list of calls so a test can assert the probe was NOT made."""
    calls = []

    def fake_probe(**kwargs):
        calls.append(kwargs)
        return True, ""

    monkeypatch.setattr(srv, "probe_local_model", fake_probe)
    return calls


@pytest.fixture
def client():
    srv.app.config["TESTING"] = True
    srv.conversations.clear()
    srv.summaries.clear()
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

# The auth boundary is enumerated from app.url_map rather than hand-listed. A
# hand-list silently stops covering what gets added after it: /api/logs,
# /api/logs/entries and /api/vault_health were all unguarded here for exactly
# that reason. Adding an exemption is now a deliberate edit to a commented set,
# not an omission nobody notices.

# Routes that deliberately do NOT answer 401 to an unauthenticated caller.
_AUTH_EXEMPT = {
    "login",       # the auth endpoint itself
    "static",      # static assets, no user data
    "bg_resolve",  # token-authenticated instead; see docs/security-model.md
}
# Page routes render the login form (200) rather than a JSON 401 — a browser
# navigation showing raw JSON is worse than showing the form.
#
# Derived from srv.VIEW_PAGES rather than hand-listed, so adding a page can't
# quietly land outside this sweep: the route and the expectation come from the
# same table. "index" ("/") is the login form itself and isn't in that table.
_LOGIN_PAGE_ENDPOINTS = {"index"} | {
    f"page{rule.replace('/', '_')}" for rule in srv.VIEW_PAGES
}
# A game's bundle is a browser navigation too, but redirects to "/" instead of
# rendering the form inline (routes_games.py avoids importing LOGIN_PAGE, which
# would be a circular import).
_REDIRECT_ENDPOINTS = {"games.game_asset"}

# Sample values for URL parameters, so a rule can be turned into a concrete
# request. A new parameter with no value here raises a KeyError rather than
# silently skipping the route — the whole point is that nothing goes uncovered.
_URL_ARGS = {
    "task_key": "morning_brief", "run_id": "someid", "item_id": "abc",
    "watch_id": "abc", "game_id": "weigh-anchor", "asset": "index.html",
    "endpoint": "decide", "filename": "favicon.svg", "name": "some-page",
}


def _routes():
    """(endpoint, method, url) for every rule that should enforce auth."""
    out = []
    for rule in srv.app.url_map.iter_rules():
        if rule.endpoint in _AUTH_EXEMPT:
            continue
        method = sorted(rule.methods - {"HEAD", "OPTIONS"})[0]
        url = rule.build({a: _URL_ARGS[a] for a in rule.arguments})[1]
        out.append((rule.endpoint, method, url))
    return sorted(out)


@pytest.mark.parametrize("endpoint,method,url", _routes())
def test_every_route_enforces_auth(client, endpoint, method, url):
    resp = getattr(client, method.lower())(url)
    if endpoint in _LOGIN_PAGE_ENDPOINTS:
        assert resp.status_code == 200
        assert "Access token" in resp.get_data(as_text=True)
    elif endpoint in _REDIRECT_ENDPOINTS:
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/"
    else:
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "not authenticated"


def test_every_view_page_is_actually_registered_and_serves_its_file(auth_client):
    """The pages are registered from a table in a loop, so a bug in the loop
    would drop routes rather than raise. Pin that each one is reachable AND
    hands back its own file — a loop that closed over the last filename would
    serve nine copies of games.html and still 200."""
    for rule, filename in srv.VIEW_PAGES.items():
        resp = auth_client.get(rule)
        assert resp.status_code == 200, rule
        expected = (srv.STATIC_DIR / filename).read_bytes()
        assert resp.get_data() == expected, rule


def test_the_auth_sweep_actually_covers_the_app():
    """Guard on the guard: if _routes() ever silently returned nothing (a bad
    filter, a rename), every case above would vacuously pass."""
    endpoints = {e for e, _, _ in _routes()}
    assert len(endpoints) > 25
    # The three that were missing from the old hand-list, pinned by name.
    assert {"logs.api_logs", "logs.api_log_entries",
            "dashboard.api_vault_health"} <= endpoints


def test_security_headers_present(client):
    resp = client.get("/")
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]


# --------------------------------------------------------------------------- #
# The chat system prompt — assembled from two soft-wrapped persona files
# --------------------------------------------------------------------------- #

def test_unwrap_collapses_soft_wraps_but_keeps_paragraph_breaks():
    assert srv._unwrap("one\ntwo") == "one two"
    assert srv._unwrap("one\n\ntwo") == "one\n\ntwo"
    assert srv._unwrap("a\nb\n\nc\nd") == "a b\n\nc d"


def test_the_tools_prompt_reaches_the_model_as_one_paragraph():
    """agent/wren_chat_tools.md is wrapped for editing, and the model must see it
    unwrapped — exactly as it did when it was a concatenated literal here. A
    stray blank line would silently split it, so pin the shape rather than the
    (frequently edited) wording."""
    head, _, tools = srv.CHAT_SYSTEM_PROMPT.partition("\n\n---\n\n")
    assert head, "the behaviour half (wren_chat.md) is missing"
    assert tools, "the tools half (wren_chat_tools.md) is missing"
    assert "\n" not in tools, "wren_chat_tools.md gained a paragraph break"
    # The placeholder is substituted, not shipped to the model verbatim.
    assert "{name}" not in srv.CHAT_SYSTEM_PROMPT
    assert srv._NAME in tools


def test_the_tools_prompt_still_names_its_confirmation_gated_tools():
    """Per CLAUDE.md, an instruction about a gated tool must tell the model to
    call it in the same turn rather than promise to — the wording that took the
    replay from 2-of-3 failing to 9 of 9. Pin that the instruction survives an
    edit to the file."""
    tools = srv.CHAT_SYSTEM_PROMPT.partition("\n\n---\n\n")[2]
    for name in ("set_reminder", "recategorize", "list_scheduled_tasks", "recall"):
        assert name in tools
    assert "call the tool" in tools


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
    assert srv._trim_history(history) == []
    assert history == before


def test_trim_drops_oldest_whole_turns_keeps_system_and_last(monkeypatch):
    monkeypatch.setattr(srv, "MAX_HISTORY_CHARS", 400)
    history = [{"role": "system", "content": "s"}, *_turn(1), *_turn(2), *_turn(3)]

    dropped = srv._trim_history(history)

    assert len(dropped) == 8  # turns 1 and 2, four messages each
    # Returned in order, so the summarizer reads them as a transcript.
    assert "question 1" in dropped[0]["content"] and "question 2" in dropped[4]["content"]
    assert history[0]["role"] == "system"
    # What survives starts at a user-message boundary: no orphaned tool result
    # or assistant half-turn at the front of the retained window.
    assert history[1]["role"] == "user" and "question 3" in history[1]["content"]
    assert [m["role"] for m in history[1:]] == ["user", "assistant", "tool", "assistant"]


def test_trim_never_drops_the_only_turn(monkeypatch):
    monkeypatch.setattr(srv, "MAX_HISTORY_CHARS", 10)  # everything is over budget
    history = [{"role": "system", "content": "s"}, *_turn(1)]
    assert srv._trim_history(history) == []
    assert len(history) == 5


def test_chat_trims_before_running_the_turn(auth_client, monkeypatch):
    monkeypatch.setattr(srv, "MAX_HISTORY_CHARS", 400)
    srv.conversations[SID] = [{"role": "system", "content": "s"},
                              *_turn(1), *_turn(2), *_turn(3)]

    def fake_advance(messages, tools, dispatch, confirm_before=frozenset(), logger=None,
                     should_cancel=None, **_):
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
# Compaction — what replaces the turns the trim evicts
# --------------------------------------------------------------------------- #


def _final_advance(monkeypatch):
    def fake_advance(messages, tools, dispatch, confirm_before=frozenset(), logger=None,
                     should_cancel=None, **_):
        return {"type": "final", "text": "done"}

    monkeypatch.setattr(srv, "advance", fake_advance)


def test_chat_compacts_dropped_turns_into_the_system_message(
        auth_client, monkeypatch, compaction_model):
    monkeypatch.setattr(srv, "MAX_HISTORY_CHARS", 400)
    monkeypatch.setattr(srv, "_system_message_content", lambda: "base prompt")
    srv.conversations[SID] = [{"role": "system", "content": "s"},
                              *_turn(1), *_turn(2), *_turn(3)]
    _final_advance(monkeypatch)

    assert auth_client.post("/chat", json={"message": "next question"}).status_code == 200

    # The dropped turns reached the summarizer...
    assert len(compaction_model) == 1
    assert "question 1" in compaction_model[0]["prompt"]
    # ...think is off: a template-filling call, where thinking tokens eat the
    # answer budget and return empty content.
    assert compaction_model[0]["think"] is False
    # ...and the summary rides in the system message, which the trim never evicts.
    system = srv.conversations[SID][0]
    assert system["role"] == "system"
    assert system["content"].startswith("base prompt")
    assert "- a summary" in system["content"]
    assert srv.summaries[SID] == "- a summary"


def test_chat_tells_the_client_it_compacted(auth_client, monkeypatch):
    """The dock renders a note from this flag. Without it the session quietly
    changes what it remembers mid-conversation and nothing on screen says so."""
    monkeypatch.setattr(srv, "MAX_HISTORY_CHARS", 400)
    srv.conversations[SID] = [{"role": "system", "content": "s"},
                              *_turn(1), *_turn(2), *_turn(3)]
    _final_advance(monkeypatch)

    resp = auth_client.post("/chat", json={"message": "next question"})

    assert resp.get_json()["compacted"] is True


def test_chat_omits_the_compacted_flag_on_an_ordinary_turn(auth_client, monkeypatch):
    srv.conversations[SID] = [{"role": "system", "content": "s"}, *_turn(1)]
    _final_advance(monkeypatch)

    resp = auth_client.post("/chat", json={"message": "hi"})

    assert "compacted" not in resp.get_json()


def test_compacted_flag_rides_out_on_a_failed_turn(auth_client, monkeypatch):
    """The history was summarized away before advance() ran, so the notice is
    owed even though the turn itself blew up."""
    monkeypatch.setattr(srv, "MAX_HISTORY_CHARS", 400)
    srv.conversations[SID] = [{"role": "system", "content": "s"},
                              *_turn(1), *_turn(2), *_turn(3)]

    def boom(*a, **k):
        raise RuntimeError("model unreachable")

    monkeypatch.setattr(srv, "advance", boom)
    resp = auth_client.post("/chat", json={"message": "next question"})

    assert resp.status_code == 500
    assert resp.get_json()["compacted"] is True


def test_compaction_summary_survives_the_next_compaction(
        auth_client, monkeypatch, compaction_model):
    """The second compaction is handed the first one's summary, so the session
    keeps one running record rather than forgetting each time it trims."""
    monkeypatch.setattr(srv, "MAX_HISTORY_CHARS", 400)
    monkeypatch.setattr(srv, "_system_message_content", lambda: "base prompt")
    srv.summaries[SID] = "- established earlier"
    srv.conversations[SID] = [{"role": "system", "content": "s"},
                              *_turn(1), *_turn(2), *_turn(3)]
    _final_advance(monkeypatch)

    assert auth_client.post("/chat", json={"message": "next question"}).status_code == 200

    assert "- established earlier" in compaction_model[0]["prompt"]


def test_compaction_keeps_the_old_summary_when_the_model_returns_nothing(
        auth_client, monkeypatch, caplog):
    """An empty model response is the small-model failure mode (thinking eats the
    budget). The turns are gone either way, so the request must still succeed —
    but it logs WARNING, because the symptom otherwise looks like the model
    losing the thread weeks later."""
    monkeypatch.setattr(srv, "MAX_HISTORY_CHARS", 400)
    monkeypatch.setattr(srv, "complete_text", lambda *a, **k: "   ")
    srv.summaries[SID] = "- established earlier"
    srv.conversations[SID] = [{"role": "system", "content": "s"},
                              *_turn(1), *_turn(2), *_turn(3)]
    _final_advance(monkeypatch)

    with caplog.at_level(logging.WARNING):
        assert auth_client.post("/chat", json={"message": "next"}).status_code == 200

    assert srv.summaries[SID] == "- established earlier"
    assert "compaction produced no summary" in caplog.text


def test_compaction_survives_a_failing_model_call(auth_client, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("ollama is busy")

    monkeypatch.setattr(srv, "MAX_HISTORY_CHARS", 400)
    monkeypatch.setattr(srv, "complete_text", boom)
    srv.conversations[SID] = [{"role": "system", "content": "s"},
                              *_turn(1), *_turn(2), *_turn(3)]
    _final_advance(monkeypatch)

    with caplog.at_level(logging.WARNING):
        assert auth_client.post("/chat", json={"message": "next"}).status_code == 200

    assert srv.summaries[SID] == ""
    assert "ollama is busy" in caplog.text


def test_summary_transcript_is_bounded(monkeypatch):
    """Both caps hold: per message, and overall. The overall cut keeps the END —
    the newest evicted messages, the ones the previous summary hasn't covered."""
    monkeypatch.setattr(srv, "SUMMARY_MESSAGE_CHARS", 10)
    monkeypatch.setattr(srv, "SUMMARY_INPUT_CHARS", 40)
    dropped = [{"role": "user", "content": f"{i} " + "x" * 500} for i in range(5)]

    transcript = srv._summary_transcript(dropped)

    assert len(transcript) <= 40
    assert transcript.endswith("4 xxxxxxxx")


def test_summarize_truncates_to_a_whole_line(monkeypatch):
    monkeypatch.setattr(srv, "SUMMARY_CHARS", 20)
    monkeypatch.setattr(srv, "complete_text", lambda *a, **k: "- one fact\n- a second fact")

    assert srv._summarize_dropped([{"role": "user", "content": "hi"}], "") == "- one fact"


def test_summary_chars_zero_disables_compaction(monkeypatch, compaction_model):
    """The documented off switch: plain dropping, and no model call at all —
    the latency on the shared Ollama slot is the reason to reach for it."""
    monkeypatch.setattr(srv, "SUMMARY_CHARS", 0)

    assert srv._summarize_dropped([{"role": "user", "content": "hi"}], "- earlier") == ""
    assert compaction_model == []


def test_chat_new_clears_the_summary(auth_client):
    srv.summaries[SID] = "- established earlier"
    assert auth_client.post("/chat/new").status_code == 200
    assert SID not in srv.summaries


def test_chat_rebuilds_system_message_every_turn(auth_client, monkeypatch):
    """A fact pinned (or skill saved) mid-session must reach the model on the
    very next turn: /chat rebuilds history[0] from _system_message_content()
    on every message, not just when the session starts."""
    srv.conversations[SID] = [{"role": "system", "content": "stale prompt"}, *_turn(1)]
    monkeypatch.setattr(srv, "_system_message_content", lambda: "fresh prompt")

    def fake_advance(messages, tools, dispatch, confirm_before=frozenset(), logger=None,
                     should_cancel=None, **_):
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
                     should_cancel=None, **_):
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
             should_cancel=None, **_):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(srv, "advance", boom)

    resp = auth_client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 500
    # The failed turn's user message is rolled back; only the seeded system
    # prompt remains, so the next turn starts from a clean, valid history.
    history = srv.conversations[SID]
    assert len(history) == 1
    assert history[0]["role"] == "system"


# --------------------------------------------------------------------------- #
# Promised-but-didn't-act detection
# --------------------------------------------------------------------------- #

def _final_reply(text):
    """An advance() double that answers with text and calls no tool."""
    def fake_advance(messages, tools, dispatch, confirm_before=frozenset(), logger=None,
                     should_cancel=None, **_):
        return {"type": "final", "text": text}
    return fake_advance


def test_chat_warns_when_the_model_promises_a_write_but_calls_no_tool(
        auth_client, monkeypatch, caplog):
    """The 2026-08-01 miss: asked to add a Strava activity to the calendar, the
    model replied that it would and emitted no tool_call, so nothing was written
    and nothing was logged. That silence is the bug being closed here."""
    monkeypatch.setattr(
        srv, "advance",
        _final_reply('I\'ll add "Evening Volleyball" to your calendar for '
                     'yesterday, July 31, from 6:38 PM to 9:13 PM.'),
    )

    with caplog.at_level(logging.WARNING, logger=srv.logger.name):
        resp = auth_client.post("/chat", json={"message": "add it to my calendar"})
    assert resp.status_code == 200
    assert "promised an action but executed no tool" in caplog.text


def test_no_promise_warning_when_the_turn_actually_ran_a_tool(
        auth_client, monkeypatch, caplog):
    def fake_advance(messages, tools, dispatch, confirm_before=frozenset(), logger=None,
                     should_cancel=None, **_):
        messages.append({"role": "assistant", "content": "", "tool_calls": []})
        messages.append({"role": "tool", "content": '{"event_id": "abc"}'})
        return {"type": "final", "text": "I'll add that — done, it's on your calendar."}

    monkeypatch.setattr(srv, "advance", fake_advance)

    with caplog.at_level(logging.WARNING, logger=srv.logger.name):
        resp = auth_client.post("/chat", json={"message": "add it to my calendar"})
    assert resp.status_code == 200
    assert "promised an action" not in caplog.text


def test_no_promise_warning_on_an_ordinary_conversational_reply(
        auth_client, monkeypatch, caplog):
    """The check must not fire on future-tense phrasing that isn't a write —
    otherwise the warning becomes noise and stops being read."""
    monkeypatch.setattr(
        srv, "advance",
        _final_reply("I'll need the tasklist_id before I can do that. "
                     "Let me know which list it's in."),
    )

    with caplog.at_level(logging.WARNING, logger=srv.logger.name):
        resp = auth_client.post("/chat", json={"message": "complete that task"})
    assert resp.status_code == 200
    assert "promised an action" not in caplog.text


def test_chat_warns_when_the_final_reply_is_empty_and_no_tool_ran(
        auth_client, monkeypatch, caplog):
    """Measured 2026-08-15: the model returns content of length 0 with
    done_reason `stop`, so nothing in agent/loop.py flags it and the turn logs
    as ordinary. The user sees an empty bubble; the log has to say why."""
    monkeypatch.setattr(srv, "advance", _final_reply(""))

    with caplog.at_level(logging.WARNING, logger=srv.logger.name):
        resp = auth_client.post("/chat", json={"message": "what's due soon?"})
    assert resp.status_code == 200
    assert "returned an EMPTY final reply and ran no tool" in caplog.text


def test_empty_final_warning_names_the_tools_the_turn_ran(
        auth_client, monkeypatch, caplog):
    """"Fetched the answer then said nothing about it" is a different bug from
    "said nothing at all", so the warning has to distinguish them."""
    def fake_advance(messages, tools, dispatch, confirm_before=frozenset(), logger=None,
                     should_cancel=None, **_):
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "get_tasks_due_soon", "arguments": {}}},
        ]})
        messages.append({"role": "tool", "content": '{"tasks": []}'})
        return {"type": "final", "text": ""}

    monkeypatch.setattr(srv, "advance", fake_advance)

    with caplog.at_level(logging.WARNING, logger=srv.logger.name):
        resp = auth_client.post("/chat", json={"message": "what's due soon?"})
    assert resp.status_code == 200
    assert "returned an EMPTY final reply after running 1 tool(s)" in caplog.text
    assert "get_tasks_due_soon" in caplog.text


def test_empty_final_warning_fires_on_a_whitespace_only_reply(
        auth_client, monkeypatch, caplog):
    monkeypatch.setattr(srv, "advance", _final_reply("  \n "))

    with caplog.at_level(logging.WARNING, logger=srv.logger.name):
        resp = auth_client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    assert "returned an EMPTY final reply" in caplog.text


def test_no_empty_final_warning_on_an_ordinary_reply(
        auth_client, monkeypatch, caplog):
    monkeypatch.setattr(srv, "advance", _final_reply("Nothing is due soon."))

    with caplog.at_level(logging.WARNING, logger=srv.logger.name):
        resp = auth_client.post("/chat", json={"message": "what's due soon?"})
    assert resp.status_code == 200
    assert "EMPTY final reply" not in caplog.text


def test_empty_final_is_still_returned_to_the_client_not_retried(
        auth_client, monkeypatch):
    """The warning is a signal, not a recovery: the turn ends as it was, with
    one advance() call, exactly like the promise-without-acting check."""
    calls = []

    def fake_advance(messages, tools, dispatch, confirm_before=frozenset(), logger=None,
                     should_cancel=None, **_):
        calls.append(1)
        return {"type": "final", "text": ""}

    monkeypatch.setattr(srv, "advance", fake_advance)

    resp = auth_client.post("/chat", json={"message": "what's due soon?"})
    assert resp.status_code == 200
    assert resp.get_json()["type"] == "final"
    assert resp.get_json()["text"] == ""
    assert len(calls) == 1


@pytest.mark.parametrize("text", [
    "I'll add Evening Volleyball to your calendar.",
    "I'm going to send that email now.",
    "Let me set a reminder for 3pm.",
    "I will create a task for that.",
    "I’ll save that to your memory.",
])
def test_promise_phrasings_are_recognised(text):
    assert srv._PROMISE_RE.search(text)


@pytest.mark.parametrize("text", [
    "I'll need more detail to do that.",
    "Let me know if you'd like it on the calendar instead.",
    "I'll explain why that date looked wrong.",
    "Added Evening Volleyball to your calendar.",
])
def test_non_promise_phrasings_are_ignored(text):
    assert not srv._PROMISE_RE.search(text)


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
             should_cancel=None, **_):
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
                  should_cancel=None, **_):
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
                     should_cancel=None, **_):
        logged_by_the_time_advance_ran.extend(records)
        return {"type": "final", "text": "done"}

    monkeypatch.setattr(srv, "advance", fake_advance)
    try:
        resp = auth_client.post("/chat", json={"message": "hi"})
    finally:
        srv.logger.removeHandler(handler)

    assert resp.status_code == 200
    assert any("chat turn start" in m for m in logged_by_the_time_advance_ran)


def test_chat_turn_uses_the_interactive_timeout(auth_client, monkeypatch):
    """An interactive turn must not inherit the scheduled tasks' patient
    OLLAMA_TIMEOUT. On 2026-08-03 a chat turn queued behind a background job
    and sat for the full 300s before reporting a bare connection error; the
    person waiting is better served by failing fast (see CHAT_MODEL_TIMEOUT)."""
    seen = {}

    def fake_advance(messages, tools, dispatch, confirm_before=frozenset(), logger=None,
                     should_cancel=None, timeout=None, **_):
        seen["timeout"] = timeout
        return {"type": "final", "text": "done"}

    monkeypatch.setattr(srv, "advance", fake_advance)
    monkeypatch.setattr(srv, "CHAT_MODEL_TIMEOUT", 42.0)
    resp = auth_client.post("/chat", json={"message": "hi"})

    assert resp.status_code == 200
    assert seen["timeout"] == 42.0


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
    srv.summaries["stale-sid"] = "- established earlier"
    srv._session_last_active["stale-sid"] = time.time() - srv.SESSION_IDLE_EVICT_S - 1
    srv.conversations["fresh-sid"] = [{"role": "system", "content": "s"}]
    srv._session_last_active["fresh-sid"] = time.time()

    monkeypatch.setattr(srv, "advance",
                        lambda *a, **k: {"type": "final", "text": "hi"})
    resp = auth_client.post("/chat", json={"message": "hello"})
    assert resp.status_code == 200

    assert "stale-sid" not in srv.conversations
    assert "stale-sid" not in srv.pending_confirmations
    assert "stale-sid" not in srv.summaries
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
    assert set(data) == {"agents", "services", "routines", "memory", "skills"}
    # Both agents ship in every payload — /map draws one at a time from them.
    assert data["agents"]["wren"]["name"] == "Wren"
    assert data["agents"]["scribejay"]["name"] == "ScribeJay"
    # Every registered chat tool lands in exactly one service group.
    grouped = [t["name"] for s in data["services"] for t in s["tools"]]
    registered = [t["function"]["name"] for t in srv.TOOLS]
    assert sorted(grouped) == sorted(registered)
    # The missing vault degrades to an empty wiki band, never an error.
    assert data["memory"]["wiki_pages"] == []
    assert [m["text"] for m in data["memory"]["entries"]] == ["Crows can recognize human faces"]
    assert data["skills"] == []
    for rt in data["routines"]:
        assert set(rt) == {"key", "display_name", "human_schedule", "next_run",
                           "last_run", "uses", "agent", "writes"}
        assert rt["agent"] in {"wren", "scribejay", "external"}


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
                     should_cancel=None, **_):
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
                     should_cancel=None, **_):
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
    assert "search_wiki" in after and "search_wiki" not in before
    assert "search_wiki" in result["now_available"]
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
                     should_cancel=None, **_):
        return {"type": "final", "text": "ok"}

    monkeypatch.setattr(srv, "advance", fake_advance)
    auth_client.post("/chat", json={"message": "anything in my wiki about that?"})
    assert "wiki" in srv.loaded_groups[SID]
    # A later plain turn keeps the group loaded (persists across turns).
    auth_client.post("/chat", json={"message": "thanks"})
    assert "wiki" in srv.loaded_groups[SID]
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
    #
    # num_ctx was 32768 until 2026-08-26. It moved because this check started
    # counting the prompt head it had been ignoring, and 32768 does not in fact
    # fit the worst case — the old green was the arithmetic being wrong, not the
    # config being safe. If registering more tools ever turns this red again,
    # that is the check working: raise OLLAMA_NUM_CTX to match.
    assert _budget(monkeypatch, history=48000, num_ctx=49152) is None


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
    # the whole point of the check. num_ctx is sized off the measured head plus
    # the history, so the ONLY term that differs between the two calls is the
    # tool results — a fixed num_ctx here would be over budget on the head alone
    # and both calls would warn for the wrong reason.
    fits = (srv._prompt_head_chars() + 16000 + 1000) // 4
    assert _budget(monkeypatch, history=16000, num_ctx=fits, iterations=10,
                   result_chars=8000) is not None
    assert _budget(monkeypatch, history=16000, num_ctx=fits, iterations=1,
                   result_chars=100) is None


def test_budget_counts_the_prompt_head(monkeypatch):
    """The head — system message, compaction summary, tool schemas — is on every
    turn and is the bigger half of the worst case. The check ignored it until
    2026-08-26 and so blessed a config that could not fit.

    Asserted as the *difference* the head makes, not as a fixed number: the
    schema total moves every time a tool is registered, and a hard-coded figure
    here would rot into a test that passes without checking anything."""
    head = srv._prompt_head_chars()
    assert head > 20000, "the head is real; a near-zero value means it stopped being measured"

    # A num_ctx that fits history + tool results EXACTLY, with nothing spare.
    # Under the old arithmetic this was the green case; the head is what tips it.
    bare = (48000 + (10 * 8000)) // 4
    warning = _budget(monkeypatch, history=48000, num_ctx=bare)
    assert warning is not None, "the head is not being counted"
    assert "prompt head" in warning and f"{head:,}" in warning

    # And the same config passes once num_ctx covers the head too — proving the
    # head is what tipped it, and not some other term.
    assert _budget(monkeypatch, history=48000, num_ctx=bare + (head // 4) + 1) is None


def test_prompt_head_prices_every_tool_group(monkeypatch):
    """Worst case means every group loaded. loaded_groups only grows within a
    session (nothing removes one short of /chat/new), so the all-groups schema
    total is the real ceiling rather than a pessimistic one."""
    core_only = sum(len(json.dumps(x)) for x in toolset.tools_for(set()))
    all_groups = sum(len(json.dumps(x)) for x in toolset.tools_for(set(toolset.TOOL_GROUPS)))
    assert all_groups > core_only, "groups must add schema, or this check is vacuous"
    assert srv._prompt_head_chars() >= all_groups


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
# /api/schedules (the dashboard's task boxes)
# --------------------------------------------------------------------------- #

def _fake_task(key, label, external=False, is_daemon=False):
    return {
        "key": key, "display_name": key.title(), "label": label,
        "module": f"tasks.{key}", "schedule": {"Hour": 6, "Minute": 0},
        "human_schedule": "Daily 6:00 AM", "log_path": pathlib.Path("/nonexistent") / f"{key}.log",
        "is_daemon": is_daemon, "external": external,
    }


def test_api_schedules_says_which_agent_owns_each_task(auth_client, monkeypatch):
    # The dashboard groups tasks into one box per agent, so every row has to
    # carry its owner. Real _agent_of runs here on purpose: stubbing it would
    # prove only that the key is copied, not that the label decides it.
    from chat import routes_dashboard
    monkeypatch.setattr(routes_dashboard, "discover_tasks", lambda: [
        _fake_task("brief", "local.wren.brief"),
        _fake_task("strava_download", "local.scribejay.stravadownload"),
        _fake_task("wiki_ingest", "local.wiki.ingest", external=True),
        _fake_task("bg_worker", "local.wren.bgworker", is_daemon=True),
    ])
    tasks = auth_client.get("/api/schedules").get_json()["tasks"]
    assert {t["key"]: t["agent"] for t in tasks} == {
        "brief": "wren",
        # A scribejay.* label is the whole difference — the module path above still
        # says tasks.*, and the answer must not come from it.
        "strava_download": "scribejay",
        "wiki_ingest": "external",
        "bg_worker": "wren",
    }
    # Every row carries it, daemons included; a box with no agent has nowhere
    # to be drawn.
    assert all("agent" in t for t in tasks)


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
             should_cancel=None, backend=None, **_):
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
             should_cancel=None, backend=None, **_):
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
                       logger=None, should_cancel=None, backend=None, **kwargs):
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


# --------------------------------------------------------------------------- #
# The busy offer — /chat asks whether the local slot is free BEFORE committing
# the turn, and offers the frontier model when it isn't.
# --------------------------------------------------------------------------- #

def _probe_says_busy(monkeypatch, reason="Wren is busy: gemma4 holds the slot."):
    monkeypatch.setattr(srv, "probe_local_model", lambda **k: (False, reason))


def _refuse_advance(monkeypatch):
    """advance() must never run on the busy path — that is the whole promise."""
    def boom(*a, **k):
        raise AssertionError("advance() ran on a turn that was never committed")
    monkeypatch.setattr(srv, "advance", boom)
    return boom


def test_busy_offers_the_frontier_model_instead_of_waiting(
        auth_client, monkeypatch, frontier_configured):
    _probe_says_busy(monkeypatch)
    _refuse_advance(monkeypatch)

    resp = auth_client.post("/chat", json={"message": "what's on today?"})

    body = resp.get_json()
    assert resp.status_code == 200
    assert body["type"] == "busy"
    assert "busy" in body["reason"]
    assert "gemini" in body["escalate_to"]


def test_busy_leaves_the_session_exactly_as_it_found_it(
        auth_client, monkeypatch, frontier_configured):
    """Three halves to one promise, so all three are asserted: no turn ran, the
    history is untouched, and the turn slot is handed back. A busy answer that
    banked the user's message would replay it on the next turn; one that kept
    the slot would 409 the very button it just offered."""
    _probe_says_busy(monkeypatch)
    _refuse_advance(monkeypatch)
    srv.conversations[SID] = [{"role": "system", "content": "s"}]

    auth_client.post("/chat", json={"message": "what's on today?"})

    assert srv.conversations[SID] == [{"role": "system", "content": "s"}]
    assert SID not in srv.cancel_events


def test_busy_leaves_the_next_turn_free_to_run(
        auth_client, monkeypatch, frontier_configured):
    """The slot release above, proved from the outside: the follow-up request
    the offer exists to make must not come back 409."""
    _probe_says_busy(monkeypatch)
    auth_client.post("/chat", json={"message": "what's on today?"})

    monkeypatch.setattr(srv, "probe_local_model", lambda **k: (True, ""))
    monkeypatch.setattr(srv, "advance",
                        lambda *a, **k: {"type": "final", "text": "local answer"})
    resp = auth_client.post("/chat", json={"message": "what's on today?"})

    assert resp.status_code == 200
    assert resp.get_json()["type"] == "final"


def test_no_probe_when_there_is_nowhere_to_escalate(auth_client, monkeypatch, busy_probe):
    """A local-only install must not pay the probe's round trip every turn:
    with no frontier backend configured there is no offer to make."""
    monkeypatch.setattr(srv, "advance",
                        lambda *a, **k: {"type": "final", "text": "local answer"})
    resp = auth_client.post("/chat", json={"message": "hi"})

    assert resp.get_json()["type"] == "final"
    assert busy_probe == []


def test_probe_skipped_when_switched_off(auth_client, monkeypatch, busy_probe,
                                         frontier_configured):
    """WREN_CHAT_BUSY_PROBE=0 is the escape hatch if the probe ever misbehaves.
    Read at import like every other setting here, so it is patched as one."""
    monkeypatch.setattr(srv, "BUSY_PROBE_ENABLED", False)
    monkeypatch.setattr(srv, "advance",
                        lambda *a, **k: {"type": "final", "text": "local answer"})
    resp = auth_client.post("/chat", json={"message": "hi"})

    assert resp.get_json()["type"] == "final"
    assert busy_probe == []


def test_wait_for_wren_skips_the_probe_and_runs_locally(
        auth_client, monkeypatch, frontier_configured):
    """The second button. Waiting is the old behaviour, and re-probing would
    just offer the same choice again in a loop."""
    _probe_says_busy(monkeypatch)
    seen = {}

    def fake_advance(messages, tools, dispatch, backend=None, **k):
        seen["backend"] = backend
        return {"type": "final", "text": "local answer"}

    monkeypatch.setattr(srv, "advance", fake_advance)
    resp = auth_client.post("/chat", json={"message": "hi", "force_local": True})

    assert resp.get_json()["type"] == "final"
    assert seen["backend"] is None  # stayed on the local model


def test_ask_the_frontier_model_runs_there_and_badges_it(
        auth_client, monkeypatch, frontier_configured):
    fake = _frontier_advance()
    monkeypatch.setattr(srv, "advance", fake)

    resp = auth_client.post("/chat", json={"message": "hi", "backend": "frontier"})

    body = resp.get_json()
    assert fake.backend == "gemini"
    assert body["escalated"] is True
    assert "gemini" in body["model_label"]


def test_frontier_turn_from_busy_is_logged_as_such(
        auth_client, monkeypatch, frontier_configured):
    """The audit trail. `trigger` separates this from a manual redo, because a
    router argued for by availability is a different feature from one argued
    for by answer quality."""
    monkeypatch.setattr(srv, "advance", _frontier_advance())

    auth_client.post("/chat", json={"message": "what's on today?",
                                    "backend": "frontier"})

    row = _escalation_rows()[-1]
    assert row["trigger"] == "busy"
    assert row["request"] == "what's on today?"
    assert row["local_reply"] == ""  # the local model never answered
    assert row["outcome"] == "ok"
    assert row["backend"] == "gemini"


def test_a_failed_frontier_turn_is_logged_as_failed(
        auth_client, monkeypatch, frontier_configured):
    """A record written before the call would claim every escalation succeeded.
    The off-device attempt happened either way, so it is logged either way."""
    def boom(*a, **k):
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(srv, "advance", boom)
    resp = auth_client.post("/chat", json={"message": "hi", "backend": "frontier"})

    assert resp.status_code == 500
    row = _escalation_rows()[-1]
    assert row["trigger"] == "busy"
    assert row["outcome"].startswith("error:")


def test_frontier_turn_refused_when_no_backend_is_configured(auth_client, monkeypatch):
    _refuse_advance(monkeypatch)
    resp = auth_client.post("/chat", json={"message": "hi", "backend": "frontier"})

    assert resp.status_code == 400
    assert "no frontier backend" in resp.get_json()["error"]
    assert SID not in srv.cancel_events


def test_frontier_turn_compacts_off_device_too(
        auth_client, monkeypatch, frontier_configured, compaction_model):
    """A turn routed around a busy local model must not queue on that same
    model to summarize its own history — that would hang the escape hatch on
    the thing it is escaping."""
    monkeypatch.setattr(srv, "advance", _frontier_advance())
    monkeypatch.setattr(srv, "MAX_HISTORY_CHARS", 200)
    srv.conversations[SID] = [{"role": "system", "content": "s"}] + [
        {"role": "user", "content": "x" * 100} for _ in range(6)
    ]

    auth_client.post("/chat", json={"message": "hi", "backend": "frontier"})

    assert compaction_model, "the history should have been compacted"
    assert compaction_model[-1]["backend"] == "gemini"
