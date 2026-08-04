"""Tests for the log inspector's two signals, its classifier, and its rollup.

discover_tasks is monkeypatched in every test that reaches Signal B: the real one
globs the production launchd/ directory, so an unstubbed call would read the user's
real plists into a fixture assertion. (Its log_path builds on chat.insights.LOGS_DIR,
which conftest now redirects suite-wide — the stub is what keeps the plists out.)

Log lines are built relative to an explicit `now` rather than hardcoded, so the
24h boundary cases can't rot.
"""

from datetime import datetime, timedelta

import pytest

from tasks import _common, log_inspector

NOW = datetime(2026, 7, 16, 8, 0, 0)


def _line(when: datetime, level: str, msg: str) -> str:
    return f"{when.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} [{level}] {msg}"


def _write_log(name: str, *lines: str) -> None:
    (_common.LOGS_DIR / name).write_text("\n".join(lines) + "\n")


def _run(when: datetime, label: str, *body: str) -> list[str]:
    return [_line(when, "INFO", f"Starting {label} run"), *body]


@pytest.fixture(autouse=True)
def _no_real_tasks(monkeypatch):
    """Signal B off unless a test opts in. Also the guard described above."""
    monkeypatch.setattr(log_inspector, "discover_tasks", lambda: [])


@pytest.fixture(autouse=True)
def _healthy_channel(monkeypatch):
    """No real ntfy probe unless a test opts in — ntfy_health does real network.
    (The probe itself lives in agent/tools/notify.py and is tested there.)"""
    monkeypatch.setattr(log_inspector, "ntfy_health",
                        lambda: {"state": "ok", "error": None})


@pytest.fixture
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(log_inspector, "notify",
                        lambda **kw: sent.append(kw) or {"ok": True})
    return sent


def _task(tmp_path, key="morning_brief", *, schedule=None, is_daemon=False) -> dict:
    return {
        "key": key,
        "display_name": key.title(),
        "schedule": schedule if schedule is not None else {"Hour": 6, "Minute": 0},
        "is_daemon": is_daemon,
        "log_path": str(tmp_path / f"{key}.log"),
    }


# --------------------------------------------------------------------------- #
# Signal A — the line scan and its classifier
# --------------------------------------------------------------------------- #

def test_error_line_is_critical():
    _write_log("wren.log", _line(NOW - timedelta(hours=2), "ERROR", "chat turn failed: boom"))
    findings = log_inspector._scan_lines(NOW)
    assert len(findings) == 1
    assert findings[0]["severity"] == "critical"
    assert findings[0]["source"] == "wren"


def test_strain_warnings_are_labelled_not_just_counted():
    _write_log(
        "wren.log",
        _line(NOW - timedelta(hours=3), "WARNING",
              "ollama prompt (35607 tokens) reached num_ctx=16384 — the front was truncated"),
        _line(NOW - timedelta(hours=2), "WARNING",
              "ollama generation (3072 tokens) reached num_predict=3072 and was cut off"),
    )
    labels = [f["label"] for f in log_inspector._scan_lines(NOW)]
    assert labels == ["context overflow", "repetition loop"]


def test_unrecognised_warning_still_surfaces():
    """Default-open: the classifier must not be an allowlist of known patterns.

    A logger.warning added anywhere later has to report on its own — this is the
    regression guard for someone 'tidying' _classify into a pattern match.
    """
    _write_log("calendar_colorizer.log",
               _line(NOW - timedelta(hours=1), "WARNING", "No valid color for event abc123, skipping"))
    findings = log_inspector._scan_lines(NOW)
    assert len(findings) == 1
    assert findings[0]["severity"] == "warn"
    assert findings[0]["label"] is None  # unlabelled, but reported


def test_warm_model_failure_is_critical_despite_warning_level():
    _write_log("wren.log",
               _line(NOW - timedelta(hours=1), "WARNING",
                     "warm_model failed (connection refused); attempting generation cold"))
    assert log_inspector._scan_lines(NOW)[0]["severity"] == "critical"


@pytest.mark.parametrize("msg", [
    "tool_call get_events_by_date result trimmed: 4654 chars over the 8000 cap",
    "login throttled for 127.0.0.1, retry after 29s",
    "bg_resolve: rejected invalid or expired token",
])
def test_noise_is_never_reported(msg):
    _write_log("wren.log", _line(NOW - timedelta(hours=1), "WARNING", msg))
    assert log_inspector._scan_lines(NOW) == []


def test_repeated_push_failures_are_not_noise():
    """Regression for the July 2026 outage: these were suppressed as 'transient
    and self-healing'. Over four days they were neither, and they were the only
    signal we had."""
    _write_log("reminder_sweep.log",
               _line(NOW - timedelta(hours=1), "WARNING",
                     "reminder ff596625 push failed, will retry: connection refused"))
    assert len(log_inspector._scan_lines(NOW)) == 1


def test_info_lines_are_ignored():
    _write_log("wren.log", _line(NOW - timedelta(hours=1), "INFO", "Starting morning brief run"))
    assert log_inspector._scan_lines(NOW) == []


def test_lines_outside_the_window_are_ignored():
    _write_log(
        "wren.log",
        _line(NOW - timedelta(hours=25), "ERROR", "yesterday's problem, already reported"),
        _line(NOW - timedelta(hours=23), "ERROR", "inside the window"),
    )
    findings = log_inspector._scan_lines(NOW)
    assert [f["msg"] for f in findings] == ["inside the window"]


def test_own_log_is_never_scanned():
    """The self-reference guard: this task logs its findings, quoting them.

    Without the exclusion the classifier's substring match re-detects yesterday's
    findings as today's problems, forever.
    """
    _write_log("log_inspector.log",
               _line(NOW - timedelta(hours=1), "INFO",
                     "finding -> wren 2026-07-15 04:00:00,000 [ERROR] chat turn failed: boom"),
               _line(NOW - timedelta(hours=1), "ERROR", "a real past error of our own"))
    assert log_inspector._scan_lines(NOW) == []


def test_launchd_mirror_is_not_double_counted():
    """setup_logger writes to the file AND stdout; launchd captures stdout into
    <task>.launchd.log, so every line exists twice on disk."""
    err = _line(NOW - timedelta(hours=1), "ERROR", "Morning brief run failed: boom")
    _write_log("morning_brief.log", err)
    _write_log("morning_brief.launchd.log", err)
    assert len(log_inspector._scan_lines(NOW)) == 1


def test_bak_files_are_not_scanned():
    _write_log("wren.log.bak", _line(NOW - timedelta(hours=1), "ERROR", "stale hand-made copy"))
    assert log_inspector._scan_lines(NOW) == []


def test_rotated_logs_are_scanned():
    """A 24h window can span a RotatingFileHandler rollover."""
    (_common.LOGS_DIR / "wren.log.1").write_text(
        _line(NOW - timedelta(hours=5), "ERROR", "pre-rotation error") + "\n")
    _write_log("wren.log", _line(NOW - timedelta(hours=1), "ERROR", "post-rotation error"))
    assert [f["msg"] for f in log_inspector._scan_lines(NOW)] == [
        "pre-rotation error", "post-rotation error"]


def test_external_root_logs_are_scanned(tmp_path, monkeypatch):
    """A sibling repo's scheduled jobs report into the same rollup — they alert
    on their own failures, but nothing else notices a stalled or wedged run."""
    root = tmp_path / "sibling"
    (root / "logs").mkdir(parents=True)
    (root / "logs" / "wiki_ingest.llm-wiki-learnings.log").write_text(
        _line(NOW - timedelta(hours=1), "ERROR", "Run budget exhausted after 45 min") + "\n")
    monkeypatch.setenv("WREN_EXTERNAL_TASK_ROOTS", f"wiki={root}")

    (finding,) = log_inspector._scan_lines(NOW)
    assert finding["msg"] == "Run budget exhausted after 45 min"
    assert finding["source"] == "wiki_ingest.llm-wiki-learnings"


def test_external_launchd_mirror_is_not_double_counted(tmp_path, monkeypatch):
    """The external repo's setup_logger mirrors to stdout too, so the same line
    lands in its .launchd.log — _skip_log has to cover both roots."""
    root = tmp_path / "sibling"
    (root / "logs").mkdir(parents=True)
    err = _line(NOW - timedelta(hours=1), "ERROR", "Run budget exhausted") + "\n"
    (root / "logs" / "wiki_ingest.llm-wiki-learnings.log").write_text(err)
    (root / "logs" / "learnings-ingest.launchd.log").write_text(err)
    monkeypatch.setenv("WREN_EXTERNAL_TASK_ROOTS", f"wiki={root}")

    assert len(log_inspector._scan_lines(NOW)) == 1


def test_traceback_continuation_does_not_double_count():
    _write_log(
        "wren.log",
        _line(NOW - timedelta(hours=1), "ERROR", "chat turn failed: boom"),
        "Traceback (most recent call last):",
        '  File "chat/server.py", line 1, in <module>',
    )
    assert len(log_inspector._scan_lines(NOW)) == 1


# --------------------------------------------------------------------------- #
# Signal B — run outcomes
# --------------------------------------------------------------------------- #

def test_task_that_never_ran_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(log_inspector, "discover_tasks", lambda: [_task(tmp_path)])
    (tmp_path / "morning_brief.log").write_text("")
    assert log_inspector._task_outcomes(NOW)["missing"] == ["morning_brief"]


def test_failed_run_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(log_inspector, "discover_tasks", lambda: [_task(tmp_path)])
    at = NOW - timedelta(hours=2)
    (tmp_path / "morning_brief.log").write_text("\n".join(_run(
        at, "morning brief",
        _line(at + timedelta(seconds=5), "ERROR", "Morning brief run failed: boom"),
    )) + "\n")
    outcomes = log_inspector._task_outcomes(NOW)
    assert outcomes["failed"] == ["morning_brief"]
    assert outcomes["missing"] == []


def test_successful_run_is_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(log_inspector, "discover_tasks", lambda: [_task(tmp_path)])
    at = NOW - timedelta(hours=2)
    (tmp_path / "morning_brief.log").write_text("\n".join(_run(
        at, "morning brief",
        _line(at + timedelta(seconds=5), "INFO", "Morning brief run complete"),
    )) + "\n")
    assert log_inspector._task_outcomes(NOW) == {"failed": [], "stalled": [], "missing": []}


def test_run_that_never_finished_is_stalled(monkeypatch, tmp_path):
    """No end line and no error: the process died without raising (SIGKILL/OOM),
    which no error line records."""
    monkeypatch.setattr(log_inspector, "discover_tasks", lambda: [_task(tmp_path)])
    at = NOW - timedelta(hours=2)
    (tmp_path / "morning_brief.log").write_text("\n".join(_run(at, "morning brief")) + "\n")
    assert log_inspector._task_outcomes(NOW)["stalled"] == ["morning_brief"]


def test_recently_started_run_is_not_yet_stalled(monkeypatch, tmp_path):
    """A slow-but-healthy run in progress must not read as dead."""
    monkeypatch.setattr(log_inspector, "discover_tasks", lambda: [_task(tmp_path)])
    at = NOW - timedelta(minutes=20)
    (tmp_path / "morning_brief.log").write_text("\n".join(_run(at, "morning brief")) + "\n")
    assert log_inspector._task_outcomes(NOW) == {"failed": [], "stalled": [], "missing": []}


def test_daemons_are_skipped(monkeypatch, tmp_path):
    """The chat server and the pollers never emit run boundaries."""
    monkeypatch.setattr(log_inspector, "discover_tasks",
                        lambda: [_task(tmp_path, "wren", schedule=None, is_daemon=True)])
    (tmp_path / "wren.log").write_text("")
    assert log_inspector._task_outcomes(NOW)["missing"] == []


def test_weekly_task_not_due_in_the_window_is_not_reported_missing(monkeypatch, tmp_path):
    """NOW is a Thursday, so a Sunday task's last due time is four days back:
    'isn't due' must not read as 'didn't run'."""
    monkeypatch.setattr(log_inspector, "discover_tasks", lambda: [
        _task(tmp_path, "weekly_thing", schedule={"Weekday": 0, "Hour": 5, "Minute": 0})])
    (tmp_path / "weekly_thing.log").write_text("")
    assert log_inspector._task_outcomes(NOW)["missing"] == []


def test_weekly_task_due_in_the_window_is_reported_missing(monkeypatch, tmp_path):
    """The gap this closes: a weekly task that was due last night and never ran
    used to be skipped outright, so nothing reported it."""
    monkeypatch.setattr(log_inspector, "discover_tasks", lambda: [
        _task(tmp_path, "weekly_thing", schedule={"Weekday": 3, "Hour": 21, "Minute": 0})])
    (tmp_path / "weekly_thing.log").write_text("")
    assert log_inspector._task_outcomes(NOW)["missing"] == ["weekly_thing"]


def test_weekly_task_that_ran_when_due_is_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(log_inspector, "discover_tasks", lambda: [
        _task(tmp_path, "weekly_thing", schedule={"Weekday": 3, "Hour": 21, "Minute": 0})])
    at = NOW - timedelta(hours=11)  # Wednesday 21:00, its due time
    (tmp_path / "weekly_thing.log").write_text("\n".join(_run(
        at, "weekly thing",
        _line(at + timedelta(seconds=5), "INFO", "Weekly thing run complete"),
    )) + "\n")
    assert log_inspector._task_outcomes(NOW) == {"failed": [], "stalled": [], "missing": []}


def test_weekly_task_failure_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(log_inspector, "discover_tasks", lambda: [
        _task(tmp_path, "weekly_thing", schedule={"Weekday": 3, "Hour": 21, "Minute": 0})])
    at = NOW - timedelta(hours=11)
    (tmp_path / "weekly_thing.log").write_text("\n".join(_run(
        at, "weekly thing",
        _line(at + timedelta(seconds=5), "ERROR", "Weekly thing run failed: boom"),
    )) + "\n")
    assert log_inspector._task_outcomes(NOW)["failed"] == ["weekly_thing"]


def test_weekly_task_is_reported_once_not_every_day(monkeypatch, tmp_path):
    """A weekly miss must not nag daily until the next occurrence. The Thursday
    08:00 run reports it; Friday and Saturday stay quiet."""
    monkeypatch.setattr(log_inspector, "discover_tasks", lambda: [
        _task(tmp_path, "weekly_thing", schedule={"Weekday": 3, "Hour": 21, "Minute": 0})])
    (tmp_path / "weekly_thing.log").write_text("")
    reported = [log_inspector._task_outcomes(NOW + timedelta(days=d))["missing"]
                for d in range(3)]
    assert reported == [["weekly_thing"], [], []]


def test_monthly_schedule_is_skipped(monkeypatch, tmp_path):
    """A schedule shape we can't place in time keeps the old silence rather than
    guessing a due date and crying wolf. Day/Month are the shapes we don't read."""
    monkeypatch.setattr(log_inspector, "discover_tasks", lambda: [
        _task(tmp_path, "odd_thing", schedule={"Day": 1, "Hour": 5, "Minute": 0})])
    (tmp_path / "odd_thing.log").write_text("")
    assert log_inspector._task_outcomes(NOW)["missing"] == []


@pytest.mark.parametrize("sci", [None, "", [{"Hour": 5}]])
def test_unreadable_schedule_has_no_due_time(sci):
    """launchd also allows a list of dicts; we don't read that shape either."""
    assert log_inspector._last_due(sci, NOW) is None


@pytest.mark.parametrize("launchd_weekday", [0, 7])
def test_sunday_is_both_zero_and_seven(launchd_weekday):
    """launchd accepts either for Sunday; both must resolve to the same day."""
    sunday_9pm = datetime(2026, 7, 19, 21, 0)
    monday_8am = datetime(2026, 7, 20, 8, 0)
    sci = {"Weekday": launchd_weekday, "Hour": 21, "Minute": 0}
    assert log_inspector._last_due(sci, monday_8am) == sunday_9pm


def test_due_time_later_today_resolves_to_last_week():
    """Sunday 08:00, for a task due Sunday 21:00: the due time is tonight, so the
    last one was a week ago — not today, which would report it missing early."""
    sci = {"Weekday": 0, "Hour": 21, "Minute": 0}
    assert log_inspector._last_due(sci, datetime(2026, 7, 19, 8, 0)) == datetime(2026, 7, 12, 21, 0)


def test_daily_due_time_later_today_resolves_to_yesterday():
    sci = {"Hour": 20, "Minute": 10}
    assert log_inspector._last_due(sci, datetime(2026, 7, 19, 8, 0)) == datetime(2026, 7, 18, 20, 10)


def test_every_real_daily_task_is_still_judged_at_the_scheduled_inspection_time():
    """A daily period always lands inside a 24h window, so the new due-time gate
    must be a no-op for dailies no matter what hour they run at."""
    now = datetime(2026, 7, 16, 8, 0)  # when the inspector actually fires
    cutoff = now - timedelta(hours=log_inspector.WINDOW_HOURS)
    for hour in range(24):
        due = log_inspector._last_due({"Hour": hour, "Minute": 0}, now)
        assert due is not None and due >= cutoff, hour


def test_yesterdays_run_does_not_count_as_today(monkeypatch, tmp_path):
    monkeypatch.setattr(log_inspector, "discover_tasks", lambda: [_task(tmp_path)])
    at = NOW - timedelta(hours=26)
    (tmp_path / "morning_brief.log").write_text("\n".join(_run(
        at, "morning brief",
        _line(at + timedelta(seconds=5), "INFO", "Morning brief run complete"),
    )) + "\n")
    assert log_inspector._task_outcomes(NOW)["missing"] == ["morning_brief"]


# --------------------------------------------------------------------------- #
# Rollup
# --------------------------------------------------------------------------- #

def test_rollup_reports_counts_not_raw_lines():
    outcomes = {"failed": ["morning_brief", "ai_chat_learnings"],
                "stalled": [], "missing": ["strava_download"]}
    findings = [
        {"source": "wren", "severity": "warn", "label": "context overflow"},
        {"source": "wren", "severity": "warn", "label": "context overflow"},
        {"source": "wren", "severity": "critical", "label": None},
        {"source": "bg_worker", "severity": "warn", "label": None},
    ]
    summary = log_inspector._rollup(outcomes, findings)
    assert "2 failed: morning_brief, ai_chat_learnings" in summary
    assert "1 didn't run: strava_download" in summary
    assert "Model strain: 2x context overflow" in summary
    assert "1 error lines: wren(1)" in summary
    assert "1 warnings: bg_worker(1)" in summary


def test_rollup_of_a_clean_window_is_empty():
    assert log_inspector._rollup({"failed": [], "stalled": [], "missing": []}, []) == ""


def test_rollup_stays_within_the_ntfy_cap():
    """notify() truncates at 500 chars — a busy night must not lose the headline."""
    outcomes = {"failed": [f"task_{i}" for i in range(8)], "stalled": [], "missing": []}
    findings = [{"source": f"src_{i % 5}", "severity": "critical", "label": None}
                for i in range(60)]
    summary = log_inspector._rollup(outcomes, findings)
    assert len(summary) < 500
    assert summary.startswith("8 failed:")


# --------------------------------------------------------------------------- #
# Push-channel health — the outage that started all this
# --------------------------------------------------------------------------- #

def test_dead_channel_is_reported_even_when_everything_else_is_clean(monkeypatch, pushes):
    """The July 2026 outage in one test.

    ntfy was down four days and not one line was logged about it, because
    nothing needed pushing. A log scan cannot see this — only an active probe
    can, which is why ntfy_health exists.
    """
    monkeypatch.setattr(log_inspector, "ntfy_health",
                        lambda: {"state": "down",
                                 "error": "ntfy unreachable: Connection refused"})
    # No error lines anywhere: the logs are spotless, the channel is dead.
    assert log_inspector.main() == 0
    assert len(pushes) == 1
    assert pushes[0]["message"].startswith("PUSH CHANNEL DOWN:")
    assert pushes[0]["priority"] == "high"


def test_dead_channel_alert_goes_out_with_email_fallback(monkeypatch, pushes):
    """The alert about the dead channel can only arrive by email — the push
    carrying it is the thing that's broken."""
    monkeypatch.setattr(log_inspector, "ntfy_health",
                        lambda: {"state": "down", "error": "ntfy unreachable: refused"})
    assert log_inspector.main() == 0
    assert pushes[0]["email_fallback"] is True


def test_disabled_push_is_not_a_fault(monkeypatch, pushes):
    """Push deliberately switched off (README) must not read as an outage. The
    probe reports "off" with no error; this task reports faults, so it stays
    quiet. (The probe's own three states are tested in test_notify.py.)"""
    monkeypatch.setattr(log_inspector, "ntfy_health",
                        lambda: {"state": "off", "error": None})
    assert log_inspector.main() == 0
    assert pushes == []


# --------------------------------------------------------------------------- #
# main() wiring
# --------------------------------------------------------------------------- #

def test_clean_window_pushes_nothing(pushes):
    _write_log("wren.log", _line(datetime.now() - timedelta(hours=1), "INFO", "all fine"))
    assert log_inspector.main() == 0
    assert pushes == []


def test_findings_push_once_and_still_exit_zero(pushes):
    """Finding problems is a successful run — only the inspector's own failure is 1."""
    _write_log("wren.log",
               _line(datetime.now() - timedelta(hours=1), "ERROR", "chat turn failed: boom"))
    assert log_inspector.main() == 0
    assert len(pushes) == 1
    assert pushes[0]["priority"] == "high"
    assert "1 error lines: wren(1)" in pushes[0]["message"]


def test_strain_only_pushes_at_default_priority(pushes):
    _write_log("wren.log",
               _line(datetime.now() - timedelta(hours=1), "WARNING",
                     "ollama generation (3072 tokens) reached num_predict=3072 and was cut off"))
    assert log_inspector.main() == 0
    assert pushes[0]["priority"] == "default"
    assert "Model strain: 1x repetition loop" in pushes[0]["message"]


def test_push_failure_does_not_fail_the_run(monkeypatch, pushes):
    monkeypatch.setattr(log_inspector, "notify", lambda **kw: {"error": "ntfy down"})
    _write_log("wren.log",
               _line(datetime.now() - timedelta(hours=1), "ERROR", "chat turn failed: boom"))
    assert log_inspector.main() == 0


def test_inspector_failure_pushes_and_exits_one(monkeypatch):
    alerts = []
    monkeypatch.setattr(log_inspector, "notify_failure",
                        lambda name, detail, logger=None: alerts.append(str(detail)))
    monkeypatch.setattr(log_inspector, "_scan_lines",
                        lambda now: (_ for _ in ()).throw(OSError("logs unreadable")))
    assert log_inspector.main() == 1
    assert any("logs unreadable" in a for a in alerts)


def test_quoted_findings_do_not_pollute_our_own_run_detail(pushes):
    """Detail lines quote other tasks' messages, which contain the word 'failed'.

    insights.py treats a line without " -> " as the run's own status line, so
    without that marker _parse_runs_uncached appends every quoted finding to this
    run's `error` field and the dashboard shows an error blob on a clean run.

    Asserting on `error` rather than `status` is deliberate: a trailing "run
    complete" always resets status back to success, so a status assertion here
    passes with or without the guard and tests nothing.
    """
    from chat.insights import parse_runs

    _write_log("wren.log",
               _line(datetime.now() - timedelta(hours=1), "ERROR",
                     "Morning brief run failed: boom"))
    assert log_inspector.main() == 0

    runs = parse_runs(_common.LOGS_DIR / "log_inspector.log")
    assert len(runs) == 1
    assert runs[0]["status"] == "success"
    assert runs[0]["error"] == ""
