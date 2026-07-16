"""Tests for the log inspector's two signals, its classifier, and its rollup.

discover_tasks is monkeypatched in every test that reaches Signal B: the real
one globs the production launchd/ directory and builds log_path against
chat.insights.LOGS_DIR (which conftest does NOT redirect), so an unstubbed call
would read Craig's real plists and real logs into a fixture assertion.

Log lines are built relative to an explicit `now` rather than hardcoded, so the
24h boundary cases can't rot.
"""

from datetime import datetime, timedelta

import pytest
import requests

from tasks import _common, log_inspector

NOW = datetime(2026, 7, 16, 8, 0, 0)

# Captured before the autouse _healthy_channel fixture stubs it out. The probe
# tests below must exercise the real function — calling the stub would make them
# pass vacuously.
_REAL_NTFY_HEALTH = log_inspector._ntfy_health


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
    """No real ntfy probe unless a test opts in — _ntfy_health does real network."""
    monkeypatch.setattr(log_inspector, "_ntfy_health", lambda: None)


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


def test_weekly_task_is_not_reported_missing(monkeypatch, tmp_path):
    """A 24h window can't tell 'didn't run' from 'isn't due' for a weekly task."""
    monkeypatch.setattr(log_inspector, "discover_tasks", lambda: [
        _task(tmp_path, "weekly_thing", schedule={"Weekday": 0, "Hour": 5, "Minute": 0})])
    (tmp_path / "weekly_thing.log").write_text("")
    assert log_inspector._task_outcomes(NOW)["missing"] == []


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
    can, which is why _ntfy_health exists.
    """
    monkeypatch.setattr(log_inspector, "_ntfy_health",
                        lambda: "ntfy unreachable: Connection refused")
    # No error lines anywhere: the logs are spotless, the channel is dead.
    assert log_inspector.main() == 0
    assert len(pushes) == 1
    assert pushes[0]["message"].startswith("PUSH CHANNEL DOWN:")
    assert pushes[0]["priority"] == "high"


def test_dead_channel_alert_goes_out_with_email_fallback(monkeypatch, pushes):
    """The alert about the dead channel can only arrive by email — the push
    carrying it is the thing that's broken."""
    monkeypatch.setattr(log_inspector, "_ntfy_health", lambda: "ntfy unreachable: refused")
    assert log_inspector.main() == 0
    assert pushes[0]["email_fallback"] is True


def test_unset_ntfy_url_is_not_a_fault(monkeypatch):
    """Push deliberately disabled (README) must not read as an outage."""
    monkeypatch.delenv("NTFY_URL", raising=False)
    monkeypatch.setattr(log_inspector, "load_env", lambda: None)
    assert _REAL_NTFY_HEALTH() is None


def test_unreachable_ntfy_is_detected(monkeypatch):
    monkeypatch.setattr(log_inspector, "load_env", lambda: None)
    monkeypatch.setenv("NTFY_URL", "http://box:2586/wren-alerts")

    def boom(url, timeout=None):
        raise requests.exceptions.ConnectionError("Connection refused")

    monkeypatch.setattr(log_inspector.requests, "get", boom)
    assert "unreachable" in _REAL_NTFY_HEALTH()


def test_healthy_ntfy_probes_the_health_endpoint_not_the_topic(monkeypatch):
    """A probe that published would alert the phone every single morning."""
    monkeypatch.setattr(log_inspector, "load_env", lambda: None)
    monkeypatch.setenv("NTFY_URL", "http://box:2586/wren-alerts")
    seen = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"healthy": True}

    def fake_get(url, timeout=None):
        seen["url"] = url
        return _Resp()

    monkeypatch.setattr(log_inspector.requests, "get", fake_get)
    assert _REAL_NTFY_HEALTH() is None
    assert seen["url"] == "http://box:2586/v1/health"  # base + health, not the topic


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
