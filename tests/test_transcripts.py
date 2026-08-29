"""Tests for scribejay/transcripts.py — parsing Claude Code and Codex Desktop
session logs plus the Gemini drop folder. All sources are redirected to tmp_path
by conftest, so these never touch real transcripts or the user's vault."""

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from scribejay import transcripts as ct

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


def _write_codex_session(lines: list[dict], name="session", meta=None,
                         mtime=None) -> Path:
    day_dir = ct.CODEX_SESSIONS_DIR / "2024" / "06" / "01"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{name}.jsonl"
    payload = {
        "id": name,
        "timestamp": _ts(1, 9),
        "cwd": "/Users/x/Projects/MyApp",
        "originator": "Codex Desktop",
        "source": "vscode",
        "thread_source": "user",
        "history_mode": "paginated",
    }
    if meta:
        payload.update(meta)
    records = [{"timestamp": _ts(1, 9), "type": "session_meta", "payload": payload}, *lines]
    path.write_text("\n".join(json.dumps(record) for record in records))
    stamp = END.timestamp() if mtime is None else mtime
    os.utime(path, (stamp, stamp))
    return path


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


def test_claude_projects_dir_honors_claude_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "alternate-claude"))
    assert ct.claude_projects_dir() == tmp_path / "alternate-claude" / "projects"


def test_fetch_codex_sessions_extracts_visible_text_and_drops_noise(caplog):
    _write_codex_session([
        {"timestamp": _ts(1, 10), "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "<environment_context>noise</environment_context>"},
                {"type": "input_text", "text": "# AGENTS.md instructions for /tmp\nnoise"},
                {"type": "input_text", "text": "Build the transcript reader"},
            ]}},
        {"timestamp": _ts(1, 10, 1), "type": "response_item", "payload": {
            "type": "message", "role": "developer",
            "content": [{"type": "input_text", "text": "DEVELOPER_NOISE"}]}},
        {"timestamp": _ts(1, 10, 2), "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "phase": "commentary",
            "content": [{"type": "output_text", "text": "I am checking the format."}]}},
        {"timestamp": _ts(1, 10, 3), "type": "reasoning", "payload": {
            "summary": [{"type": "summary_text", "text": "PRIVATE_REASONING"}]}},
        {"timestamp": _ts(1, 10, 4), "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text", "text": "Implemented and tested it."}]}},
        # Visible text outside the selected local day must not leak in.
        {"timestamp": _ts(2, 10), "type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "NEXT_DAY"}]}},
    ])

    with caplog.at_level(logging.WARNING):
        sessions = ct.fetch_codex_sessions(START, END, logger=logging.getLogger("codex-test"))

    assert len(sessions) == 1
    session = sessions[0]
    assert session["project"] == "MyApp"
    assert session["slug"] == ""
    assert session["started_at"] == datetime(2024, 6, 1, 10, 0, tzinfo=timezone.utc)
    assert "User: Build the transcript reader" in session["text"]
    assert "Assistant: I am checking the format." in session["text"]
    assert "Assistant: Implemented and tested it." in session["text"]
    assert "environment_context" not in session["text"]
    assert "AGENTS.md" not in session["text"]
    assert "DEVELOPER_NOISE" not in session["text"]
    assert "PRIVATE_REASONING" not in session["text"]
    assert "NEXT_DAY" not in session["text"]
    assert caplog.text == ""


def test_fetch_codex_sessions_excludes_imported_onboarding_and_children():
    message = {"timestamp": _ts(1, 10), "type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "must not appear"}]}}
    _write_codex_session([message], "imported", {
        "thread_source": None, "history_mode": "legacy",
    })
    _write_codex_session([message], "onboarding", {"thread_source": "onboarding_checklist"})
    _write_codex_session([message], "guardian", {
        "thread_source": "guardian_review", "source": {"subagent": {"other": "guardian"}},
    })

    assert ct.fetch_codex_sessions(START, END) == []
    assert ct.fetch_codex_session_activity(START, END) == []


def test_fetch_codex_sessions_sorts_and_compacts():
    _write_codex_session([{"timestamp": _ts(1, 14), "type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "L" * 100}]}}], "late")
    _write_codex_session([{"timestamp": _ts(1, 9), "type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "E" * 100}]}}], "early")

    sessions = ct.fetch_codex_sessions(START, END, max_chars=60)

    assert [session["started_at"].hour for session in sessions] == [9, 14]
    assert all("trimmed" in session["text"] for session in sessions)


def test_fetch_codex_sessions_skips_stale_file():
    _write_codex_session([{"timestamp": _ts(1, 10), "type": "response_item", "payload": {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "stale"}]}}],
        mtime=START.timestamp() - 1)
    assert ct.fetch_codex_sessions(START, END) == []
    assert ct.fetch_codex_session_activity(START, END) == []


def test_fetch_codex_sessions_warns_on_private_format_drift(caplog):
    _write_codex_session([{"timestamp": _ts(1, 10), "type": "response_item", "payload": {
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "unphased answer"}]}}])

    with caplog.at_level(logging.WARNING):
        assert ct.fetch_codex_sessions(
            START, END, logger=logging.getLogger("codex-test")
        ) == []
        activity = ct.fetch_codex_session_activity(
            START, END, logger=logging.getLogger("codex-test")
        )

    assert "unknown phase" in caplog.text
    assert "no extractable turns" in caplog.text
    assert len(activity) == 2
    assert all(event["text"] is None for event in activity)


def test_fetch_codex_sessions_warns_on_malformed_metadata(caplog):
    day_dir = ct.CODEX_SESSIONS_DIR / "2024" / "06" / "01"
    day_dir.mkdir(parents=True)
    path = day_dir / "broken.jsonl"
    path.write_text("not json\n")
    os.utime(path, (END.timestamp(), END.timestamp()))

    with caplog.at_level(logging.WARNING):
        assert ct.fetch_codex_sessions(
            START, END, logger=logging.getLogger("codex-test")
        ) == []
        assert ct.fetch_codex_session_activity(
            START, END, logger=logging.getLogger("codex-test")
        ) == []
    assert "malformed metadata" in caplog.text


def test_fetch_codex_sessions_empty_when_store_absent():
    assert ct.fetch_codex_sessions(START, END) == []
    assert ct.fetch_codex_session_activity(START, END) == []


def test_codex_sessions_dir_honors_codex_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "alternate-codex"))
    assert ct.codex_sessions_dir() == tmp_path / "alternate-codex" / "sessions"


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
    monkeypatch.setenv("WREN_GEMINI_CHATS_DIR", "~/Vaults/llm-wiki-learnings/gemini_inbox")
    resolved = ct.gemini_dir()
    assert "~" not in str(resolved)
    assert resolved == Path.home() / "Vaults" / "llm-wiki-learnings" / "gemini_inbox"


def test_fetch_session_activity_keeps_every_timestamped_event():
    _write_session([
        {"timestamp": _ts(1, 10, 0), "cwd": "/Users/x/Projects/MyApp", "slug": "fix login",
         "message": {"role": "user", "content": "Please fix the login bug"}},
        # tool traffic carries no text but IS working time — the difference from
        # fetch_claude_sessions, which drops these entirely.
        {"timestamp": _ts(1, 10, 2), "toolUseResult": {"ok": True},
         "message": {"role": "user", "content": [{"type": "tool_result", "content": "out"}]}},
        {"timestamp": _ts(1, 10, 5), "isSidechain": True,
         "message": {"role": "assistant", "content": [{"type": "text", "text": "subagent"}]}},
        # no timestamp at all — nothing to place on a timeline
        {"message": {"role": "user", "content": "undated"}},
        {"timestamp": _ts(2, 10, 0), "message": {"role": "user", "content": "next day"}},
    ])

    events = ct.fetch_session_activity(START, END)

    assert [e["ts"].hour for e in events] == [10, 10, 10]
    assert [e["ts"].minute for e in events] == [0, 2, 5]
    assert {e["project"] for e in events} == {"MyApp"}
    assert {e["slug"] for e in events} == {"fix login"}
    assert {e["session"] for e in events} == {"sess"}
    # Only the first event said anything a human wrote.
    assert [bool(e["text"]) for e in events] == [True, False, False]


def test_fetch_codex_session_activity_keeps_every_top_level_timestamp():
    _write_codex_session([
        {"timestamp": _ts(1, 10), "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Build the activity reader"},
            ]}},
        {"timestamp": _ts(1, 10, 2), "type": "reasoning", "payload": {
            "summary": [{"type": "summary_text", "text": "PRIVATE_REASONING"}]}},
        {"timestamp": _ts(1, 10, 4), "type": "response_item", "payload": {
            "type": "function_call", "name": "exec_command", "arguments": "{}"}},
        {"timestamp": _ts(1, 10, 6), "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "phase": "commentary",
            "content": [{"type": "output_text", "text": "Checking the records."}]}},
        {"timestamp": _ts(2, 10), "type": "event_msg", "payload": {}},
        {"type": "event_msg", "payload": {}},
    ])

    events = ct.fetch_codex_session_activity(START, END)

    # session_meta itself is timestamped and therefore starts the activity beat.
    assert [(event["ts"].hour, event["ts"].minute) for event in events] == [
        (9, 0), (10, 0), (10, 2), (10, 4), (10, 6),
    ]
    assert {event["project"] for event in events} == {"MyApp"}
    assert {event["slug"] for event in events} == {""}
    assert {event["session"] for event in events} == {"session"}
    assert [bool(event["text"]) for event in events] == [False, True, False, False, True]
    assert "PRIVATE_REASONING" not in "\n".join(event["text"] or "" for event in events)


def test_fetch_codex_session_activity_sorts_across_tasks():
    _write_codex_session([
        {"timestamp": _ts(1, 14), "type": "event_msg", "payload": {}},
    ], "late", {"timestamp": _ts(1, 14)})
    _write_codex_session([
        {"timestamp": _ts(1, 9), "type": "event_msg", "payload": {}},
    ], "early")

    events = ct.fetch_codex_session_activity(START, END)

    assert [event["ts"].hour for event in events] == [9, 9, 9, 14]


def test_fetch_codex_session_activity_uses_the_callers_local_day():
    local_tz = timezone(timedelta(hours=-4))
    local_start = datetime(2024, 6, 1, 0, 0, tzinfo=local_tz)
    local_end = datetime(2024, 6, 1, 23, 59, 59, tzinfo=local_tz)
    _write_codex_session([
        # 10 PM on June 1 locally: included even though its UTC date is June 2.
        {"timestamp": _ts(2, 2), "type": "event_msg", "payload": {}},
        # 1 AM on June 2 locally: outside the selected local day.
        {"timestamp": _ts(2, 5), "type": "event_msg", "payload": {}},
    ])

    events = ct.fetch_codex_session_activity(local_start, local_end)

    assert events[-1]["ts"] == datetime(2024, 6, 1, 22, 0, tzinfo=local_tz)


def test_fetch_session_activity_falls_back_to_the_project_dir_name():
    # Real session files sometimes carry no cwd on any of a day's events; the
    # per-project dir name is the cwd with its separators flattened.
    _write_session([
        {"timestamp": _ts(1, 9, 0), "message": {"role": "user", "content": "hi"}},
    ])
    assert ct.fetch_session_activity(START, END)[0]["project"] == "MyApp"


def test_fetch_session_activity_sorts_across_sessions():
    project_dir = ct.CLAUDE_PROJECTS_DIR / "-Users-x-Projects-MyApp"
    project_dir.mkdir(parents=True)
    for name, hour in (("late.jsonl", 14), ("early.jsonl", 9)):
        path = project_dir / name
        path.write_text(json.dumps(
            {"timestamp": _ts(1, hour), "cwd": f"/Users/x/Projects/{name}",
             "message": {"role": "user", "content": "hi"}}))
        os.utime(path, (END.timestamp(), END.timestamp()))

    # Concurrent sessions have to interleave on one timeline — the whole point of
    # pooling them is that idle gaps are a property of the day, not of a file.
    assert [e["ts"].hour for e in ct.fetch_session_activity(START, END)] == [9, 14]


def test_fetch_session_activity_empty_when_store_absent():
    assert ct.fetch_session_activity(START, END) == []


def test_compact_trims_the_middle():
    out = ct._compact(["X" * 200], max_chars=60)
    assert out.startswith("X")
    assert "trimmed" in out
