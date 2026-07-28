"""Tests for chat.insights — the pure schedule/run-history parsing layer.

Covers the launchd Sun=0/7 weekday mapping in next_run, the '->' guard in
_is_run_success, run grouping (success/failure/running), and rotated-file
chronological ordering. `now` is pinned so next_run is deterministic.
"""

import plistlib
from datetime import datetime, timedelta

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
    assert insights._is_run_start("Starting Strava download RERUN for 2026-07-02")


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


def test_tool_result_saying_failed_does_not_fail_the_run(tmp_path):
    # A tool RESULT containing "failed" is the tool reporting an error the run
    # may recover from — the run itself completed. It must parse as a clean
    # success, not a failure with spurious error text.
    log = tmp_path / "task.log"
    _write_log(log, [
        "2026-07-07 06:00:00,000 [INFO] Starting task run",
        "2026-07-07 06:00:01,000 [INFO] fetch_strava(x) -> {'error': 'token refresh failed'}",
        "2026-07-07 06:00:05,000 [INFO] Task run complete",
    ])
    runs = insights.parse_runs(log)
    assert runs[0]["status"] == "success"
    assert runs[0]["error"] == ""


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


# --------------------------------------------------------------------------- #
# discover_tasks caching (signature-keyed on the plist directory)
# --------------------------------------------------------------------------- #

def _write_plist(path, sci):
    data = {
        "Label": f"com.test.{path.stem}",
        "ProgramArguments": ["python", "-m", f"tasks.{path.stem}"],
        "StandardOutPath": str(path.parent / f"{path.stem}.launchd.log"),
        "StartCalendarInterval": sci,
    }
    with open(path, "wb") as fh:
        plistlib.dump(data, fh)


def test_interval_poller_shows_interval_but_stays_daemon(tmp_path, monkeypatch):
    # StartInterval pollers (bg_worker, reminder_sweep) get a truthful label,
    # but keep is_daemon=True on purpose: that's what blocks the dashboard's
    # "Run now" (a manually spawned poller could race launchd's copy and pick
    # up the same job twice).
    monkeypatch.setattr(insights, "LAUNCHD_DIR", tmp_path)
    insights._TASKS_CACHE.clear()
    data = {
        "Label": "com.test.bg_worker",
        "ProgramArguments": ["python", "-m", "tasks.bg_worker"],
        "StandardOutPath": str(tmp_path / "bg_worker.launchd.log"),
        "StartInterval": 30,
    }
    with open(tmp_path / "bg_worker.plist", "wb") as fh:
        plistlib.dump(data, fh)

    (task,) = insights.discover_tasks()
    assert task["is_daemon"] is True
    assert task["human_schedule"] == "Every 30s (poll)"
    insights._TASKS_CACHE.clear()


def test_discover_tasks_reuses_cache_and_invalidates(tmp_path, monkeypatch):
    monkeypatch.setattr(insights, "LAUNCHD_DIR", tmp_path)
    insights._TASKS_CACHE.clear()
    _write_plist(tmp_path / "foo.plist", {"Hour": 6, "Minute": 0})

    calls = {"n": 0}
    real = insights._discover_tasks_uncached

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(insights, "_discover_tasks_uncached", counting)

    assert len(insights.discover_tasks()) == 1
    insights.discover_tasks()
    insights.discover_tasks()
    assert calls["n"] == 1  # parsed once, then served from cache

    # Adding a plist changes the directory signature -> cache is bypassed.
    _write_plist(tmp_path / "bar.plist", {"Hour": 7, "Minute": 0})
    assert len(insights.discover_tasks()) == 2
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# system_map
# --------------------------------------------------------------------------- #

def _isolate_map_sources(tmp_path, monkeypatch):
    """Point every system_map source at empty tmp locations so the tests never
    read the real memory store, skills dir, vault, plists, or logs."""
    from agent.tools import memory
    monkeypatch.setattr(memory, "_STORE_PATH", tmp_path / "wren_memory.json")
    monkeypatch.setenv("WREN_SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path / "no-vault"))
    monkeypatch.setattr(insights, "LAUNCHD_DIR", tmp_path)
    monkeypatch.setattr(insights, "LOGS_DIR", tmp_path)
    insights._TASKS_CACHE.clear()


SAMPLE_TOOLS = [
    {"function": {"name": "send_email", "description": "Send an email",
                  "parameters": {"type": "object", "properties": {}}}},
    {"function": {"name": "fetch_weather", "description": "Read weather",
                  "parameters": {"type": "object", "properties": {}}}},
    {"function": {"name": "totally_new_tool", "description": "Not mapped yet",
                  "parameters": {"type": "object", "properties": {}}}},
]


def test_system_map_groups_tools_by_service(tmp_path, monkeypatch):
    _isolate_map_sources(tmp_path, monkeypatch)
    out = insights.system_map(SAMPLE_TOOLS, write_tools=["send_email"])
    by_key = {s["key"]: s for s in out["services"]}
    assert [t["name"] for t in by_key["gmail"]["tools"]] == ["send_email"]
    assert by_key["gmail"]["tools"][0]["mutates"] is True
    assert [t["name"] for t in by_key["weather"]["tools"]] == ["fetch_weather"]
    assert by_key["weather"]["tools"][0]["mutates"] is False
    # An unmapped tool falls into "other" rather than disappearing.
    assert [t["name"] for t in by_key["other"]["tools"]] == ["totally_new_tool"]


def test_system_map_no_other_bucket_when_all_mapped(tmp_path, monkeypatch):
    _isolate_map_sources(tmp_path, monkeypatch)
    out = insights.system_map(SAMPLE_TOOLS[:2], write_tools=[])
    assert "other" not in {s["key"] for s in out["services"]}


def test_routine_uses_reference_defined_services():
    for task_key, services in insights.ROUTINE_USES.items():
        for key in services:
            assert key in insights.TOOL_SERVICES, f"{task_key} uses unknown service {key!r}"


def test_every_scheduled_routine_has_routine_uses():
    # Drift guard for the hand-maintained ROUTINE_USES map: adding a scheduled
    # task (a non-daemon launchd plist) without declaring the services it touches
    # here leaves it floating with no edges on the /map view. Reads the real
    # launchd/ plists — no production runtime state is touched.
    scheduled = {t["key"] for t in insights.discover_tasks() if not t["is_daemon"]}
    undeclared = scheduled - set(insights.ROUTINE_USES)
    assert not undeclared, f"routines missing from ROUTINE_USES: {sorted(undeclared)}"


def test_every_registered_chat_tool_is_mapped_to_a_service():
    # Drift guard for the hard-coded TOOL_SERVICES map: registering a new tool
    # in chat/server.py without mapping it here should fail loudly, not let the
    # tool silently pile up in the map's "other" bucket.
    import os
    os.environ.setdefault("WREN_CHAT_TOKEN", "test-token")
    os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
    from chat import server as srv
    mapped = {name for _, names in insights.TOOL_SERVICES.values() for name in names}
    registered = {t["function"]["name"] for t in srv.TOOLS}
    assert registered <= mapped, f"unmapped tools: {sorted(registered - mapped)}"


def test_system_map_routines_exclude_daemons(tmp_path, monkeypatch):
    _isolate_map_sources(tmp_path, monkeypatch)
    _write_plist(tmp_path / "foo.plist", {"Hour": 6, "Minute": 0})
    daemon = {
        "Label": "com.test.daemon",
        "ProgramArguments": ["python", "-m", "chat.server"],
        "StandardOutPath": str(tmp_path / "wren.launchd.log"),
        "KeepAlive": True,
    }
    with open(tmp_path / "daemon.plist", "wb") as fh:
        plistlib.dump(daemon, fh)

    out = insights.system_map(SAMPLE_TOOLS, write_tools=[])
    assert [rt["key"] for rt in out["routines"]] == ["foo"]
    rt = out["routines"][0]
    assert rt["human_schedule"] == "Daily 6:00 AM"
    assert rt["last_run"] is None  # no log written
    assert rt["uses"] == []        # not in ROUTINE_USES


def test_system_map_memory_and_wiki_degrade_gracefully(tmp_path, monkeypatch):
    _isolate_map_sources(tmp_path, monkeypatch)
    from agent.tools import memory
    memory.remember("x" * 500, category="trivia")
    out = insights.system_map(SAMPLE_TOOLS, write_tools=[])
    entry = out["memory"]["entries"][0]
    assert len(entry["text"]) == insights._MEMORY_TEXT_MAX + 1  # truncated + ellipsis
    assert entry["text"].endswith("…")
    assert entry["scope"] == "archival"
    # Vault dir doesn't exist -> empty wiki band, not an error payload.
    assert out["memory"]["wiki_pages"] == []
    assert out["skills"] == []


def test_discover_tasks_returns_fresh_list(tmp_path, monkeypatch):
    monkeypatch.setattr(insights, "LAUNCHD_DIR", tmp_path)
    insights._TASKS_CACHE.clear()
    _write_plist(tmp_path / "foo.plist", {"Hour": 6, "Minute": 0})

    a = insights.discover_tasks()
    b = insights.discover_tasks()
    assert a == b
    assert a is not b  # mutating one caller's result can't corrupt the cache
    a.clear()
    assert len(insights.discover_tasks()) == 1


# --------------------------------------------------------------------------- #
# run_stats (the dashboard's duration charts)
# --------------------------------------------------------------------------- #

def _stub_tasks(monkeypatch, *tasks):
    """Pin discover_tasks so run_stats reads only the fixture logs."""
    monkeypatch.setattr(insights, "discover_tasks", lambda: list(tasks))


def _task(key, log_path, is_daemon=False):
    return {"key": key, "display_name": insights._prettify(key),
            "log_path": log_path, "is_daemon": is_daemon,
            "schedule": {"Hour": 6, "Minute": 0}, "human_schedule": "Daily 6:00 AM"}


# run_stats windows on `now`, so every fixture below is dated relative to this.
STATS_NOW = datetime(2026, 7, 30, 12, 0)


def test_run_stats_series_is_oldest_first(tmp_path, monkeypatch):
    # parse_runs returns most-recent-first; a chart plots left to right, so
    # run_stats has to hand back the reverse.
    log = tmp_path / "brief.log"
    _write_log(log, [
        "2026-07-28 06:00:00,000 [INFO] Starting brief run",
        "2026-07-28 06:00:05,000 [INFO] Brief run complete",
        "2026-07-29 06:00:00,000 [INFO] Starting brief run",
        "2026-07-29 06:00:09,000 [INFO] Brief run complete",
    ])
    _stub_tasks(monkeypatch, _task("brief", log))
    task = insights.run_stats(days=30, now=STATS_NOW)["tasks"][0]
    assert [r["start"][:10] for r in task["runs"]] == ["2026-07-28", "2026-07-29"]
    assert [r["duration_s"] for r in task["runs"]] == [5.0, 9.0]


def test_run_stats_excludes_runs_outside_the_window(tmp_path, monkeypatch):
    log = tmp_path / "brief.log"
    _write_log(log, [
        "2026-06-01 06:00:00,000 [INFO] Starting brief run",   # 59 days back
        "2026-06-01 06:00:05,000 [INFO] Brief run complete",
        "2026-07-29 06:00:00,000 [INFO] Starting brief run",
        "2026-07-29 06:00:09,000 [INFO] Brief run complete",
    ])
    _stub_tasks(monkeypatch, _task("brief", log))
    out = insights.run_stats(days=7, now=STATS_NOW)
    assert out["days"] == 7
    task = out["tasks"][0]
    assert task["count"] == 1
    assert task["runs"][0]["duration_s"] == 9.0


def test_run_stats_reports_median_and_max(tmp_path, monkeypatch):
    # The whole point of the chart: a task whose runs all "succeed" can still
    # range from seconds to minutes, and only max makes that visible.
    log = tmp_path / "brief.log"
    lines = []
    for day, secs in ((26, 8), (27, 10), (28, 12), (29, 600)):
        lines += [
            f"2026-07-{day} 06:00:00,000 [INFO] Starting brief run",
            f"2026-07-{day} 06:{secs // 60:02d}:{secs % 60:02d},000 [INFO] Brief run complete",
        ]
    _write_log(log, lines)
    _stub_tasks(monkeypatch, _task("brief", log))
    task = insights.run_stats(days=30, now=STATS_NOW)["tasks"][0]
    assert task["count"] == 4
    assert task["median_s"] == 11.0
    assert task["max_s"] == 600.0


def test_run_stats_keeps_unfinished_runs_but_excludes_them_from_stats(tmp_path, monkeypatch):
    # A run that started and never closed has no duration. Dropping it would
    # hide the one state worth noticing, so it stays in the series with
    # duration_s=None and is counted separately.
    log = tmp_path / "brief.log"
    _write_log(log, [
        "2026-07-28 06:00:00,000 [INFO] Starting brief run",
        "2026-07-28 06:00:20,000 [INFO] Brief run complete",
        "2026-07-29 06:00:00,000 [INFO] Starting brief run",
    ])
    _stub_tasks(monkeypatch, _task("brief", log))
    task = insights.run_stats(days=30, now=STATS_NOW)["tasks"][0]
    assert task["count"] == 2
    assert task["unfinished"] == 1
    assert task["runs"][-1]["status"] == "running"
    assert task["runs"][-1]["duration_s"] is None
    assert task["median_s"] == 20.0
    assert task["max_s"] == 20.0


def test_run_stats_counts_failures(tmp_path, monkeypatch):
    log = tmp_path / "brief.log"
    _write_log(log, [
        "2026-07-28 06:00:00,000 [INFO] Starting brief run",
        "2026-07-28 06:00:05,000 [INFO] Brief run complete",
        "2026-07-29 06:00:00,000 [INFO] Starting brief run",
        "2026-07-29 06:00:03,000 [ERROR] Boom exploded",
    ])
    _stub_tasks(monkeypatch, _task("brief", log))
    task = insights.run_stats(days=30, now=STATS_NOW)["tasks"][0]
    assert task["failures"] == 1
    assert task["runs"][-1]["status"] == "failure"
    assert task["runs"][-1]["duration_s"] == 3.0


def test_run_stats_skips_daemons(tmp_path, monkeypatch):
    log = tmp_path / "wren.log"
    _write_log(log, ["2026-07-29 06:00:00,000 [INFO] Starting Wren chat server on port 8420"])
    _stub_tasks(monkeypatch, _task("wren", log, is_daemon=True))
    assert insights.run_stats(days=30, now=STATS_NOW)["tasks"] == []


def test_run_stats_lists_a_task_with_no_runs_in_the_window(tmp_path, monkeypatch):
    # Present but empty, so the chart can say "no runs in the last N days"
    # rather than silently omitting a task the table above still lists.
    log = tmp_path / "quiet.log"
    _write_log(log, [
        "2026-06-01 06:00:00,000 [INFO] Starting quiet run",
        "2026-06-01 06:00:05,000 [INFO] Quiet run complete",
    ])
    _stub_tasks(monkeypatch, _task("quiet", log))
    task = insights.run_stats(days=7, now=STATS_NOW)["tasks"][0]
    assert task["runs"] == []
    assert task["count"] == 0
    assert task["median_s"] is None
    assert task["max_s"] is None


def _many_runs(log, n, first_day=1):
    """n successful runs, one per day, ending the day before STATS_NOW.

    Dates come from real arithmetic, not f-string day numbers: 40 runs from
    July 1 would otherwise emit "2026-07-32", which _parse_ts rejects, and the
    runs would vanish from the series rather than failing the fixture.
    """
    start = datetime(2026, 7, first_day, 6, 0)
    lines = []
    for i in range(n):
        day = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        lines += [
            f"{day} 06:00:00,000 [INFO] Starting brief run",
            f"{day} 06:00:{(i % 59) + 1:02d},000 [INFO] Brief run complete",
        ]
    _write_log(log, lines)


def test_run_stats_caps_the_series_at_the_most_recent_runs(tmp_path, monkeypatch):
    # Past ~57 points the dots overlap into a smear that still renders and says
    # nothing, so the series is capped. Recent shape is what's read, so it's the
    # OLD end that goes.
    log = tmp_path / "brief.log"
    _many_runs(log, 12, first_day=1)
    _stub_tasks(monkeypatch, _task("brief", log))
    task = insights.run_stats(days=365, limit=5, now=STATS_NOW)["tasks"][0]
    assert task["count"] == 5
    assert [r["start"][:10] for r in task["runs"]] == [
        "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-11", "2026-07-12"]


def test_run_stats_reports_the_pre_cap_total(tmp_path, monkeypatch):
    # Without this the caller can't tell a trimmed chart from a complete one,
    # and the caption would claim a window the chart doesn't cover.
    log = tmp_path / "brief.log"
    _many_runs(log, 12, first_day=1)
    _stub_tasks(monkeypatch, _task("brief", log))
    task = insights.run_stats(days=365, limit=5, now=STATS_NOW)["tasks"][0]
    assert task["total"] == 12
    assert task["count"] == 5


def test_run_stats_statistics_describe_only_the_plotted_runs(tmp_path, monkeypatch):
    # A caption must never cite a max that isn't drawn: the dropped runs are the
    # long ones here, so max has to come down with them.
    log = tmp_path / "brief.log"
    _write_log(log, [
        "2026-07-01 06:00:00,000 [INFO] Starting brief run",
        "2026-07-01 06:10:00,000 [INFO] Brief run complete",      # 600s, dropped
        "2026-07-02 06:00:00,000 [INFO] Starting brief run",
        "2026-07-02 06:00:20,000 [ERROR] Boom",                   # failure, dropped
        "2026-07-03 06:00:00,000 [INFO] Starting brief run",
        "2026-07-03 06:00:04,000 [INFO] Brief run complete",
        "2026-07-04 06:00:00,000 [INFO] Starting brief run",
        "2026-07-04 06:00:08,000 [INFO] Brief run complete",
    ])
    _stub_tasks(monkeypatch, _task("brief", log))
    task = insights.run_stats(days=365, limit=2, now=STATS_NOW)["tasks"][0]
    assert task["max_s"] == 8.0        # not 600.0
    assert task["median_s"] == 6.0     # over the plotted pair only
    assert task["failures"] == 0       # the failed run was trimmed off


def test_run_stats_defaults_to_a_cap_of_30(tmp_path, monkeypatch):
    log = tmp_path / "brief.log"
    _many_runs(log, 31, first_day=1)
    _stub_tasks(monkeypatch, _task("brief", log))
    out = insights.run_stats(days=365, now=STATS_NOW)
    assert out["limit"] == 30
    assert out["tasks"][0]["count"] == 30
    assert out["tasks"][0]["total"] == 31


def test_run_stats_limit_none_disables_the_cap(tmp_path, monkeypatch):
    log = tmp_path / "brief.log"
    _many_runs(log, 40, first_day=1)
    _stub_tasks(monkeypatch, _task("brief", log))
    task = insights.run_stats(days=365, limit=None, now=STATS_NOW)["tasks"][0]
    assert task["count"] == task["total"] == 40


def test_run_stats_under_the_cap_reports_total_equal_to_count(tmp_path, monkeypatch):
    log = tmp_path / "brief.log"
    _many_runs(log, 3, first_day=1)
    _stub_tasks(monkeypatch, _task("brief", log))
    task = insights.run_stats(days=365, limit=30, now=STATS_NOW)["tasks"][0]
    assert task["count"] == task["total"] == 3
