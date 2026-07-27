"""Tests for tasks/_chat_transcripts.py — parsing Claude Code session logs and the
Gemini drop folder. Both sources are redirected to tmp_path by conftest, so these
never touch the real ~/.claude transcripts or the user's vault."""

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

from tasks import _chat_transcripts as ct

DAY = date(2024, 6, 1)
START = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
END = datetime(2024, 6, 1, 23, 59, 59, tzinfo=timezone.utc)


def _ts(day, hour, minute=0):
    return datetime(2024, 6, day, hour, minute, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _write_session(lines: list[dict]) -> None:
    project_dir = ct.CLAUDE_PROJECTS_DIR / "-Users-x-Projects-MyApp"
    project_dir.mkdir(parents=True)
    path = project_dir / "sess.jsonl"
    path.write_text("\n".join(json.dumps(rec) for rec in lines))
    # Pin mtime inside the day so the append-only prefilter can't skip it,
    # independent of the machine's wall clock.
    os.utime(path, (END.timestamp(), END.timestamp()))


def test_fetch_claude_sessions_extracts_text_and_drops_noise():
    _write_session([
        {"timestamp": _ts(1, 10, 0), "cwd": "/Users/x/Projects/MyApp", "slug": "fix login",
         "message": {"role": "user",
                     "content": "Please fix the <system-reminder>ignore me</system-reminder>login bug"}},
        {"timestamp": _ts(1, 10, 1),
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "Fixed the login bug."},
             {"type": "tool_use", "name": "Edit", "input": {}}]}},
        # tool result echoed as role=user — dropped via toolUseResult
        {"timestamp": _ts(1, 10, 2), "toolUseResult": {"ok": True},
         "message": {"role": "user", "content": [{"type": "tool_result", "content": "SECRET_TOOL_OUTPUT"}]}},
        # subagent sidechain — dropped
        {"timestamp": _ts(1, 10, 3), "isSidechain": True,
         "message": {"role": "assistant", "content": [{"type": "text", "text": "SIDECHAIN_NOISE"}]}},
        # next day — outside the window
        {"timestamp": _ts(2, 10, 0), "message": {"role": "user", "content": "NEXTDAY stuff"}},
    ])

    sessions = ct.fetch_claude_sessions(START, END)
    assert len(sessions) == 1
    s = sessions[0]
    assert s["project"] == "MyApp"
    assert s["slug"] == "fix login"
    assert s["started_at"] == datetime(2024, 6, 1, 10, 0, tzinfo=timezone.utc)
    assert "User: Please fix the login bug" in s["text"]   # reminder stripped
    assert "Assistant: Fixed the login bug." in s["text"]
    assert "SECRET_TOOL_OUTPUT" not in s["text"]
    assert "SIDECHAIN_NOISE" not in s["text"]
    assert "NEXTDAY" not in s["text"]


def test_fetch_claude_sessions_empty_when_store_absent():
    # conftest points CLAUDE_PROJECTS_DIR at a tmp dir that this test never creates.
    assert ct.fetch_claude_sessions(START, END) == []


def test_fetch_gemini_chats_unprocessed_and_dedup():
    d = ct.gemini_dir()
    d.mkdir(parents=True)
    (d / "a.md").write_text("chat A content")
    (d / "b.txt").write_text("chat B content")
    (d / "note.png").write_text("not a transcript")  # wrong extension
    (d / "empty.md").write_text("   ")               # blank

    res = ct.fetch_gemini_chats({})
    assert {r["name"] for r in res} == {"a.md", "b.txt"}

    a_mtime = (d / "a.md").stat().st_mtime
    res2 = ct.fetch_gemini_chats({"a.md": a_mtime})
    assert {r["name"] for r in res2} == {"b.txt"}  # already-processed a.md skipped


def test_fetch_gemini_chats_empty_when_folder_absent():
    assert ct.fetch_gemini_chats({}) == []


def test_gemini_dir_expands_a_tilde_path(monkeypatch):
    # .env.example documents this var with a ~ prefix. Unexpanded, the literal
    # "~/..." dir never exists and every dropped chat is silently skipped.
    monkeypatch.setenv("WREN_GEMINI_CHATS_DIR", "~/Documents/llm-wiki-learnings/gemini_inbox")
    resolved = ct.gemini_dir()
    assert "~" not in str(resolved)
    assert resolved == Path.home() / "Documents" / "llm-wiki-learnings" / "gemini_inbox"


def test_compact_trims_the_middle():
    out = ct._compact(["X" * 200], max_chars=60)
    assert out.startswith("X")
    assert "trimmed" in out
