"""Tests for agent/nudge_actions.py — the one-tap actions on a synthesis nudge.

The store is redirected by conftest; LEARNINGS_DIR is too, so a "wiki" action
writes into tmp_path. ClickUp and the page fetch are stubbed per test.
"""

from datetime import date, datetime, timedelta

import pytest

from agent import nudge_actions as na


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.setenv("WREN_PUBLIC_URL", "https://host")
    monkeypatch.setattr(na, "fetch_webpage", lambda **k: {"error": "offline"})


def _sig(kind="browsed", text="DuckDB docs (duckdb.org) /docs", urls=None):
    return {"channel": "chrome", "kind": kind, "text": text,
            "urls": urls or [], "tokens": set()}


def _anchor_pair(kind="wiki page", label="duckdb-analytics"):
    return {"kind": "anchor", "signal": _sig(), "anchor": {"kind": kind, "label": label}}


def _cross_pair():
    return {"kind": "cross", "signal": _sig(),
            "other": {"channel": "youtube", "kind": "watched", "text": "DuckDB talk"}}


def test_action_follows_the_anchor_kind():
    assert na.action_for(_anchor_pair("wiki page")) == "wiki"
    assert na.action_for(_anchor_pair("project you're building")) == "wiki"
    assert na.action_for(_anchor_pair("idea you parked")) == "ticket"
    assert na.action_for(_anchor_pair("company you track")) is None
    assert na.action_for(_cross_pair()) == "wiki"


def test_record_keeps_order_and_skips_nudges_with_no_action():
    rows = na.record(date(2026, 10, 30), [
        ("a", _anchor_pair()), ("b", None), ("c", _anchor_pair("company you track"))])
    assert rows[0]["action"] == "wiki" and rows[1] is None and rows[2] is None
    assert len(na._load()["nudges"]) == 1


def test_buttons_are_numbered_by_the_nudge_they_belong_to():
    rows = na.record(date(2026, 10, 30), [
        ("a", None), ("b", _anchor_pair("idea you parked", "Ideas board"))])
    actions = na.buttons(rows)
    assert len(actions) == 1
    assert actions[0]["label"] == "2 → ticket"
    assert actions[0]["method"] == "POST"
    assert actions[0]["url"].startswith("https://host/api/nudges/act?token=")


def test_no_public_url_means_no_buttons(monkeypatch):
    monkeypatch.delenv("WREN_PUBLIC_URL")
    assert na.buttons(na.record(date(2026, 10, 30), [("a", _anchor_pair())])) is None


def test_token_round_trips_and_rejects_garbage():
    assert na.read_token(na.make_token("abc")) == "abc"
    assert na.read_token("garbage") is None


def test_wiki_note_holds_facts_and_never_the_question(tmp_path):
    [row] = na.record(date(2026, 10, 30),
                      [("It fits your note; save it to the wiki?", _anchor_pair())])
    result = na.perform(row["id"])
    assert result["done"]
    [note] = list(tmp_path.glob("Wren-Nudge-*-2026-10-30.md"))
    body = note.read_text()
    assert "Relates to: [[duckdb-analytics]]" in body
    assert "- browsed: DuckDB docs (duckdb.org) /docs" in body
    assert "save it to the wiki?" not in body


def test_wiki_note_carries_the_fetched_page_text(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(na, "fetch_webpage",
                        lambda **k: seen.update(k) or {"markdown": "DuckDB is columnar."})
    pair = _anchor_pair()
    pair["signal"]["urls"] = ["https://duckdb.org/docs"]
    [row] = na.record(date(2026, 10, 30), [("x", pair)])
    na.perform(row["id"])
    assert seen["url"] == "https://duckdb.org/docs"
    assert seen["max_chars"] == na.MAX_PAGE_CHARS
    body = next(tmp_path.glob("Wren-Nudge-*.md")).read_text()
    assert "## Page text (https://duckdb.org/docs)\nDuckDB is columnar." in body


def test_ticket_action_comments_on_the_parked_item(monkeypatch):
    calls = []
    monkeypatch.setattr(na, "comment_on_clickup_task",
                        lambda title, comment: calls.append((title, comment)) or {"ok": True})
    [row] = na.record(date(2026, 10, 30), [("x", _anchor_pair("idea you parked", "Ideas board"))])
    assert na.perform(row["id"])["done"]
    assert calls[0][0] == "Ideas board"
    assert "- browsed: DuckDB docs" in calls[0][1]


def test_a_second_tap_does_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(na, "comment_on_clickup_task",
                        lambda title, comment: calls.append(title) or {"ok": True})
    [row] = na.record(date(2026, 10, 30), [("x", _anchor_pair("idea you parked"))])
    na.perform(row["id"])
    assert na.perform(row["id"]) == {"already": True, "row": na._load()["nudges"][0]}
    assert len(calls) == 1


def test_a_failed_action_stays_open_for_a_retry(monkeypatch):
    monkeypatch.setattr(na, "comment_on_clickup_task", lambda *a: {"error": "down"})
    [row] = na.record(date(2026, 10, 30), [("x", _anchor_pair("idea you parked"))])
    assert na.perform(row["id"])["error"] == "down"
    assert na._load()["nudges"][0]["done"] is None


def test_unknown_or_expired_nudge_is_an_error():
    assert "error" in na.perform("nope")


def test_old_rows_are_pruned_on_write():
    [row] = na.record(date(2026, 10, 20), [("x", _anchor_pair())])
    data = na._load()
    data["nudges"][0]["created"] = (datetime.now() - timedelta(days=8)).isoformat()
    na.atomic_write_json(na._STORE_PATH, data)
    na.record(date(2026, 10, 30), [("y", _anchor_pair())])
    assert [n["text"] for n in na._load()["nudges"]] == ["y"]
