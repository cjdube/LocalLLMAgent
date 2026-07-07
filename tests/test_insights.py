"""Tests for chat.insights — the pure schedule/run-history parsing layer.

Covers the launchd Sun=0/7 weekday mapping in next_run, the '->' guard in
_is_run_success, run grouping (success/failure/running), and rotated-file
chronological ordering. `now` is pinned so next_run is deterministic.
"""

from datetime import datetime

from chat import insights

# A Tuesday, 08:00 local.
NOW = datetime(2026, 7, 7, 8, 0)


# --------------------------------------------------------------------------- #
# human_schedule / _fmt_time
# --------------------------------------------------------------------------- #

def test_human_schedule_none():
    assert insights.human_schedule(None) == "—"


def test_human_schedule_daily():
    assert insights.human_schedule({"Hour": 6, "Minute": 5}) == "Daily 6:05 AM"


def test_human_schedule_weekday():
    got = insights.human_schedule({"Weekday": 1, "Hour": 13, "Minute": 0})
    assert got == "Mondays 1:00 PM"


def test_human_schedule_sunday_zero():
    got = insights.human_schedule({"Weekday": 0, "Hour": 9, "Minute": 30})
    assert got == "Sundays 9:30 AM"


def test_fmt_time_midnight_and_noon():
    assert insights._fmt_time(0, 0) == "12:00 AM"
    assert insights._fmt_time(12, 0) == "12:00 PM"


# --------------------------------------------------------------------------- #
# next_run
# --------------------------------------------------------------------------- #

def test_next_run_none_for_daemon():
    assert insights.next_run(None, now=NOW) is None


def test_next_run_daily_later_today():
    # 09:30 is still ahead of 08:00 now -> same day.
    assert insights.next_run({"Hour": 9, "Minute": 30}, now=NOW) == "2026-07-07T09:30"


def test_next_run_daily_already_passed_rolls_to_tomorrow():
    # 06:00 already passed at 08:00 -> next is tomorrow.
    assert insights.next_run({"Hour": 6, "Minute": 0}, now=NOW) == "2026-07-08T06:00"


def test_next_run_weekday_sunday_zero():
    # launchd Weekday 0 == Sunday; next Sunday after Tue 2026-07-07 is 07-12.
    got = insights.next_run({"Weekday": 0, "Hour": 8, "Minute": 0}, now=NOW)
    assert got == "2026-07-12T08:00"


def test_next_run_weekday_seven_is_also_sunday():
    # Weekday 7 must map to the same day as Weekday 0.
    got_zero = insights.next_run({"Weekday": 0, "Hour": 8, "Minute": 0}, now=NOW)
    got_seven = insights.next_run({"Weekday": 7, "Hour": 8, "Minute": 0}, now=NOW)
    assert got_seven == got_zero == "2026-07-12T08:00"


def test_next_run_weekday_monday():
    # launchd Weekday 1 == Monday; next Monday after Tue 2026-07-07 is 07-13.
    got = insights.next_run({"Weekday": 1, "Hour": 8, "Minute": 0}, now=NOW)
    assert got == "2026-07-13T08:00"


# --------------------------------------------------------------------------- #
# _is_run_start / _is_run_success (the '->' guard)
# --------------------------------------------------------------------------- #

def test_is_run_start_matches_run_and_rerun():
    assert insights._is_run_start("Starting morning brief run")
    assert insights._is_run_start("Starting daily log RERUN for 2026-07-02")


def test_is_run_start_excludes_server_boot():
    assert not insights._is_run_start("Starting Wren chat server on port 8420")


def test_is_run_success_true():
    assert insights._is_run_success("Morning brief run complete")


def test_is_run_success_ignores_tool_result_lines():
    # A tool result containing 'complete' must not be mistaken for the boundary.
    assert not insights._is_run_success("some_tool(x) -> task run complete")


def test_is_run_success_requires_run_word():
    assert not insights._is_run_success("download complete")


# --------------------------------------------------------------------------- #
# _parse_tool_call
# --------------------------------------------------------------------------- #

def test_parse_tool_call_with_prefix_and_args():
    got = insights._parse_tool_call("tool_call fetch_weather(location=NH) -> {'temp': 70}")
    assert got == {"name": "fetch_weather", "args": "location=NH", "result": "{'temp': 70}"}


def test_parse_tool_call_bare_name():
    got = insights._parse_tool_call("send_email -> ok")
    assert got == {"name": "send_email", "args": "", "result": "ok"}


def test_parse_tool_call_requires_arrow():
    assert insights._parse_tool_call("just a log line") is None


# --------------------------------------------------------------------------- #
# parse_runs
# --------------------------------------------------------------------------- #

def _write_log(path, lines):
    path.write_text("\n".join(lines) + "\n")


def test_parse_runs_success(tmp_path):
    log = tmp_path / "morning_brief.log"
    _write_log(log, [
        "2026-07-07 06:00:00,000 [INFO] Starting morning brief run",
        "2026-07-07 06:00:01,000 [INFO] fetch_weather(x) -> {'ok': 1}",
        "2026-07-07 06:00:05,000 [INFO] Morning brief run complete",
    ])
    runs = insights.parse_runs(log)
    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "success"
    assert run["duration_s"] == 5.0
    assert run["end"] == "2026-07-07 06:00:05,000"
    assert len(run["tool_calls"]) == 1
    assert run["tool_calls"][0]["name"] == "fetch_weather"


def test_parse_runs_failure_on_error_level(tmp_path):
    log = tmp_path / "task.log"
    _write_log(log, [
        "2026-07-07 06:00:00,000 [INFO] Starting task run",
        "2026-07-07 06:00:02,000 [ERROR] Boom exploded",
    ])
    runs = insights.parse_runs(log)
    assert len(runs) == 1
    assert runs[0]["status"] == "failure"
    assert "Boom exploded" in runs[0]["error"]


def test_parse_runs_failure_on_failed_keyword(tmp_path):
    log = tmp_path / "task.log"
    _write_log(log, [
        "2026-07-07 06:00:00,000 [INFO] Starting task run",
        "2026-07-07 06:00:02,000 [INFO] the upload failed midway",
    ])
    runs = insights.parse_runs(log)
    assert runs[0]["status"] == "failure"


def test_parse_runs_running_when_never_completed(tmp_path):
    log = tmp_path / "task.log"
    _write_log(log, [
        "2026-07-07 06:00:00,000 [INFO] Starting task run",
        "2026-07-07 06:00:01,000 [INFO] fetch(x) -> ok",
    ])
    runs = insights.parse_runs(log)
    assert runs[0]["status"] == "running"
    assert runs[0]["end"] is None


def test_parse_runs_continuation_lines_attach_to_error(tmp_path):
    log = tmp_path / "task.log"
    _write_log(log, [
        "2026-07-07 06:00:00,000 [INFO] Starting task run",
        "2026-07-07 06:00:02,000 [ERROR] Traceback (most recent call last):",
        '  File "x.py", line 1, in <module>',
        "ValueError: nope",
    ])
    runs = insights.parse_runs(log)
    assert "ValueError: nope" in runs[0]["error"]


def test_parse_runs_most_recent_first_across_rotated_files(tmp_path):
    # <name>.log.1 is older; <name>.log is newer. Result is most-recent first.
    older = tmp_path / "task.log.1"
    newer = tmp_path / "task.log"
    _write_log(older, [
        "2026-07-05 06:00:00,000 [INFO] Starting task run",
        "2026-07-05 06:00:01,000 [INFO] task run complete",
    ])
    _write_log(newer, [
        "2026-07-06 06:00:00,000 [INFO] Starting task run",
        "2026-07-06 06:00:01,000 [INFO] task run complete",
    ])
    runs = insights.parse_runs(newer)
    assert len(runs) == 2
    assert runs[0]["start"].startswith("2026-07-06")
    assert runs[1]["start"].startswith("2026-07-05")


def test_parse_runs_limit(tmp_path):
    log = tmp_path / "task.log"
    lines = []
    for day in range(1, 4):
        lines += [
            f"2026-07-0{day} 06:00:00,000 [INFO] Starting task run",
            f"2026-07-0{day} 06:00:01,000 [INFO] task run complete",
        ]
    _write_log(log, lines)
    assert len(insights.parse_runs(log, limit=2)) == 2


# --------------------------------------------------------------------------- #
# describe_tools
# --------------------------------------------------------------------------- #

def test_describe_tools_flattens_and_flags_mutations():
    tools = [
        {"function": {
            "name": "send_email",
            "description": "Send an email",
            "parameters": {
                "type": "object",
                "properties": {"to": {"type": "string", "description": "recipient"}},
                "required": ["to"],
            },
        }},
        {"function": {
            "name": "fetch_weather",
            "description": "Read weather",
            "parameters": {"type": "object", "properties": {}},
        }},
    ]
    out = insights.describe_tools(tools, write_tools=["send_email"])
    by_name = {t["name"]: t for t in out}
    assert by_name["send_email"]["mutates"] is True
    assert by_name["fetch_weather"]["mutates"] is False
    to_param = by_name["send_email"]["parameters"][0]
    assert to_param == {"name": "to", "type": "string", "description": "recipient", "required": True}
    # Read tools sort before write (mutating) tools.
    assert out[0]["name"] == "fetch_weather"


# --------------------------------------------------------------------------- #
# parse_runs caching (signature-keyed; invalidates on file change)
# --------------------------------------------------------------------------- #

def _one_run(day):
    return [
        f"2026-07-0{day} 06:00:00,000 [INFO] Starting task run",
        f"2026-07-0{day} 06:00:01,000 [INFO] task run complete",
    ]


def test_parse_runs_returns_fresh_list_each_call(tmp_path):
    log = tmp_path / "fresh.log"
    _write_log(log, _one_run(1))
    a = insights.parse_runs(log)
    b = insights.parse_runs(log)
    assert a == b
    assert a is not b  # a mutation of one result can't corrupt the cached copy
    a.clear()
    assert len(insights.parse_runs(log)) == 1


def test_parse_runs_reuses_cache_when_unchanged(tmp_path, monkeypatch):
    log = tmp_path / "cached.log"
    _write_log(log, _one_run(1))
    calls = {"n": 0}
    real = insights._parse_runs_uncached

    def counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(insights, "_parse_runs_uncached", counting)
    insights.parse_runs(log)
    insights.parse_runs(log)
    insights.parse_runs(log, limit=1)
    assert calls["n"] == 1  # parsed once, then served from cache


def test_parse_runs_invalidates_when_log_changes(tmp_path):
    log = tmp_path / "changing.log"
    _write_log(log, _one_run(1))
    assert len(insights.parse_runs(log)) == 1
    # Appending a second run grows the file, so the signature changes and the
    # cache is bypassed on the next call.
    _write_log(log, _one_run(1) + _one_run(2))
    assert len(insights.parse_runs(log)) == 2
