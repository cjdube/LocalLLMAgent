"""Tests for tasks/ai_chat_learnings.py — main() summarizes each Claude session and
new Gemini drop file into one dated file; empty/all-"None" days write nothing;
--backfill runs each day separately and skips Gemini. Collaborators are stubbed;
no model, transcript, vault, or Gmail access."""

import sys
from datetime import datetime

import pytest

from agent.store import load_json
from tasks import ai_chat_learnings as ai


@pytest.fixture
def stubbed(monkeypatch):
    seen = {"persists": []}
    monkeypatch.setattr(sys, "argv", ["ai_chat_learnings"])  # argparse must not see pytest's argv
    monkeypatch.setattr(ai, "resolve_backend", lambda key: None)
    monkeypatch.setattr(ai, "warm_model", lambda **k: True)
    monkeypatch.setattr(ai, "complete_text",
                        lambda **k: "**Accomplished**\n- Built the thing\n**Learned**\n- A useful fact")
    monkeypatch.setattr(ai, "persist_or_email",
                        lambda content, prefix, day, subject, task_name, logger:
                        seen["persists"].append({"prefix": prefix, "day": day, "content": content})
                        or {"written": True})
    monkeypatch.setattr(ai, "notify_failure", lambda *a, **k: None)
    monkeypatch.setattr(ai, "fetch_gemini_chats", lambda *a, **k: [])
    return seen


def _session():
    return {"project": "MyApp", "slug": "fix login",
            "started_at": datetime(2024, 6, 1, 9, 14), "text": "User: hi\nAssistant: done"}


def test_happy_path_writes_one_dated_file(stubbed, monkeypatch):
    monkeypatch.setattr(ai, "fetch_claude_sessions", lambda *a, **k: [_session()])
    assert ai.main() == 0
    assert len(stubbed["persists"]) == 1
    p = stubbed["persists"][0]
    assert p["prefix"] == "AI-Chat-Learnings"
    assert p["content"].startswith("## AI Chat Learnings:")
    assert "### Claude · MyApp · fix login · 9:14 AM" in p["content"]
    assert "**Accomplished**" in p["content"]


def test_no_chats_writes_nothing(stubbed, monkeypatch):
    monkeypatch.setattr(ai, "fetch_claude_sessions", lambda *a, **k: [])
    assert ai.main() == 0
    assert stubbed["persists"] == []


def test_all_none_summaries_write_nothing(stubbed, monkeypatch):
    monkeypatch.setattr(ai, "fetch_claude_sessions", lambda *a, **k: [_session()])
    monkeypatch.setattr(ai, "complete_text",
                        lambda **k: "**Accomplished**\n- None\n**Learned**\n- None")
    assert ai.main() == 0
    assert stubbed["persists"] == []  # nothing substantive → no file


def test_gemini_chat_summarized_and_marked_processed(stubbed, monkeypatch):
    monkeypatch.setattr(ai, "fetch_claude_sessions", lambda *a, **k: [])
    monkeypatch.setattr(ai, "fetch_gemini_chats",
                        lambda processed, max_chars: [{"name": "chatA.md", "mtime": 123.0, "text": "stuff"}])
    assert ai.main() == 0
    assert "### Gemini · chatA" in stubbed["persists"][0]["content"]
    state = load_json(ai.STATE_PATH, {})
    assert state["gemini_processed"]["chatA.md"] == 123.0  # won't be re-summarized next run


def test_backfill_runs_each_day_and_skips_gemini(stubbed, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ai_chat_learnings", "--backfill", "3"])
    monkeypatch.setattr(ai, "fetch_claude_sessions", lambda *a, **k: [_session()])

    def _boom(*a, **k):
        raise AssertionError("Gemini drop folder must not be read during backfill")
    monkeypatch.setattr(ai, "fetch_gemini_chats", _boom)

    assert ai.main() == 0
    assert len(stubbed["persists"]) == 3
    assert len({p["day"] for p in stubbed["persists"]}) == 3  # three distinct days


def test_single_date_runs_that_day_and_skips_gemini(stubbed, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ai_chat_learnings", "--date", "2026-06-29"])
    monkeypatch.setattr(ai, "fetch_claude_sessions", lambda *a, **k: [_session()])

    def _boom(*a, **k):
        raise AssertionError("Gemini drop folder must not be read for a single --date run")
    monkeypatch.setattr(ai, "fetch_gemini_chats", _boom)

    assert ai.main() == 0
    assert len(stubbed["persists"]) == 1
    assert str(stubbed["persists"][0]["day"]) == "2026-06-29"


def test_failure_is_a_failed_run(stubbed, monkeypatch):
    calls = []
    monkeypatch.setattr(ai, "fetch_claude_sessions",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(ai, "notify_failure",
                        lambda name, detail, logger=None: calls.append(str(detail)))
    assert ai.main() == 1
    assert any("boom" in c for c in calls)
