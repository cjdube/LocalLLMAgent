"""Tests for chat/routes_logs.py — the Flask edge of the log viewer.

The reader itself is covered by tests/test_logview.py and the rendering by
tests/log-view.test.js; what's tested here is only what the route adds, which is
all input handling: an unknown key 404s rather than silently showing a different
file, an unrecognized stream falls back instead of erroring, and the numeric
parameters are clamped rather than validated (they're view state — a nonsense
value should narrow the page, not 400 the view that asked for it).

read_log is stubbed to a recorder so these assert on what the route passed down,
not on log parsing. WREN_LOGS_DIR is redirected to tmp_path suite-wide by
tests/conftest.py, so nothing here reads the real logs/.
"""

import os

os.environ.setdefault("WREN_CHAT_TOKEN", "test-token")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import pytest

from chat import routes_logs as rl
from chat import server as srv


@pytest.fixture
def auth_client():
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:
        with c.session_transaction() as sess:
            sess["authenticated"] = True
            sess["sid"] = "test-sid"
        yield c


@pytest.fixture
def recorded(monkeypatch):
    """Capture read_log's kwargs; return an empty-but-valid page."""
    seen = {}

    def _read_log(key, **kwargs):
        seen["key"] = key
        seen.update(kwargs)
        return {"key": key, "entries": [], "counts": {}, "matched": 0,
                "scanned": {"from": 0, "to": 0, "entries": 0,
                            "complete": True, "skipped": False},
                "next_before": None, "next_after": 0}
    monkeypatch.setattr(rl, "read_log", _read_log)
    return seen


def test_list_returns_the_catalogue(auth_client, monkeypatch):
    monkeypatch.setattr(rl, "list_logs", lambda: [{"key": "wren", "streams": {}}])
    body = auth_client.get("/api/logs").get_json()
    assert body["logs"] == [{"key": "wren", "streams": {}}]


def test_unknown_key_is_404_not_a_different_file(auth_client, monkeypatch):
    # Silently showing a file other than the one asked for is worse than an error,
    # which is why key is the one parameter that is validated rather than clamped.
    monkeypatch.setattr(rl, "read_log", lambda *a, **k: None)
    resp = auth_client.get("/api/logs/entries?key=nope")
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "unknown log"


def test_an_unrecognized_stream_falls_back_to_log(auth_client, recorded):
    auth_client.get("/api/logs/entries?key=wren&stream=../../etc/passwd")
    assert recorded["stream"] == "log"


def test_stdout_stream_is_passed_through(auth_client, recorded):
    auth_client.get("/api/logs/entries?key=wren&stream=stdout")
    assert recorded["stream"] == "stdout"


def test_a_missing_limit_uses_the_default(auth_client, recorded):
    auth_client.get("/api/logs/entries?key=wren")
    assert recorded["limit"] == rl.DEFAULT_LIMIT


def test_a_junk_limit_narrows_rather_than_400ing(auth_client, recorded):
    # type=int yields None for junk, which falls through to the default — the
    # page still renders instead of erroring on its own query string.
    resp = auth_client.get("/api/logs/entries?key=wren&limit=abc")
    assert resp.status_code == 200
    assert recorded["limit"] == rl.DEFAULT_LIMIT


def test_after_wins_over_before(auth_client, recorded):
    # Passing both is meaningless; `after` is the one a live-tail poll sends, so
    # it takes precedence and `before` is dropped.
    auth_client.get("/api/logs/entries?key=wren&before=500&after=100")
    assert recorded["after"] == 100
    assert recorded["before"] is None


def test_before_is_used_when_after_is_absent(auth_client, recorded):
    auth_client.get("/api/logs/entries?key=wren&before=500")
    assert recorded["before"] == 500
    assert recorded["after"] is None


def test_a_zero_after_is_honored_not_treated_as_missing(auth_client, recorded):
    # after=0 means "tail from the top of the window" and is falsy, so a truthy
    # check here would silently turn a live tail into a backwards page.
    auth_client.get("/api/logs/entries?key=wren&after=0")
    assert recorded["after"] == 0


def test_a_nonpositive_before_is_dropped(auth_client, recorded):
    auth_client.get("/api/logs/entries?key=wren&before=0")
    assert recorded["before"] is None


def test_level_and_query_filters_are_passed_through(auth_client, recorded):
    auth_client.get("/api/logs/entries?key=wren&level=warning&q=ollama")
    assert recorded["level"] == "warning"
    assert recorded["query"] == "ollama"
