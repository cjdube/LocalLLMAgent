"""Tests for serialized, dependency-aware launchd catch-up after reboot."""

from datetime import datetime, timedelta
from pathlib import Path
import plistlib

from tasks import startup_recovery as recovery


NOW = datetime(2026, 8, 29, 6, 10)


def _policy(label, task_name="task", priority=0, kind="none"):
    return {label: (task_name, priority, kind)}


def test_enqueue_deduplicates_and_keeps_unknown_visible(monkeypatch):
    monkeypatch.setattr(recovery, "POLICIES", _policy("local.wren.one"))

    result = recovery.enqueue(["local.wren.one", "local.wren.one", "unknown"], NOW)

    assert result == {"added": ["local.wren.one"], "unknown": ["unknown"]}
    assert recovery.status()["queued"][0]["label"] == "local.wren.one"


def test_queue_prefers_high_priority_then_oldest(monkeypatch):
    monkeypatch.setattr(recovery, "POLICIES", {
        "local.wren.low": ("low", 2, "none"),
        "local.wren.high-old": ("old", 0, "none"),
        "local.wren.high-new": ("new", 0, "none"),
    })
    recovery.enqueue(["local.wren.low"], NOW)
    recovery.enqueue(["local.wren.high-old"], NOW + timedelta(seconds=1))
    recovery.enqueue(["local.wren.high-new"], NOW + timedelta(seconds=2))
    started = []
    monkeypatch.setattr(recovery, "launch_status", lambda label: {"running": False, "exit_code": 0})
    monkeypatch.setattr(recovery, "start", lambda label: started.append(label) or True)

    assert recovery.run_once(NOW + timedelta(seconds=3)) == {"status": "started", "label": "local.wren.high-old"}
    assert started == ["local.wren.high-old"]


def test_ollama_wait_does_not_block_non_model_work(monkeypatch):
    monkeypatch.setattr(recovery, "POLICIES", {
        "local.wren.model": ("model", 0, "wren"),
        "local.wren.plain": ("plain", 1, "none"),
    })
    recovery.enqueue(["local.wren.model", "local.wren.plain"], NOW)
    monkeypatch.setattr(recovery, "ollama_ready", lambda: False)
    monkeypatch.setattr(recovery, "launch_status", lambda label: {"running": False, "exit_code": 0})
    started = []
    monkeypatch.setattr(recovery, "start", lambda label: started.append(label) or True)

    result = recovery.run_once(NOW)

    assert result == {"status": "started", "label": "local.wren.plain"}
    assert started == ["local.wren.plain"]


def test_active_job_completes_then_emits_one_summary(monkeypatch):
    monkeypatch.setattr(recovery, "POLICIES", _policy("local.wren.one"))
    recovery.enqueue(["local.wren.one"], NOW)
    monkeypatch.setattr(recovery, "launch_status", lambda label: {"running": False, "exit_code": 0})
    monkeypatch.setattr(recovery, "start", lambda label: True)
    recovery.run_once(NOW)
    sent = []
    from agent.tools import notify
    monkeypatch.setattr(notify, "notify", lambda *a, **k: sent.append((a, k)) or {"ok": True})

    result = recovery.run_once(NOW + timedelta(minutes=1))

    assert result["status"] == "finished"
    assert "Startup recovery completed: 1 task(s) caught up." == result["summary"]
    assert len(sent) == 1
    assert recovery.status()["active"] is None
    assert recovery.status()["queued"] == []


def test_failed_active_job_retries_then_becomes_terminal(monkeypatch):
    monkeypatch.setattr(recovery, "POLICIES", _policy("local.wren.one"))
    recovery.enqueue(["local.wren.one"], NOW)
    monkeypatch.setattr(recovery, "launch_status", lambda label: {"running": False, "exit_code": 9})
    monkeypatch.setattr(recovery, "start", lambda label: True)
    monkeypatch.setattr(recovery, "ollama_ready", lambda: True)

    recovery.run_once(NOW)
    recovery.run_once(NOW + timedelta(minutes=1))
    queued = recovery.status()["queued"]
    assert queued[0]["attempts"] == 1

    for minute in (10, 20):
        recovery.run_once(NOW + timedelta(minutes=minute))
        recovery.run_once(NOW + timedelta(minutes=minute + 1))

    assert recovery.status()["queued"] == []
    assert recovery.status()["results"][0]["result"] == "failed"


def test_start_failure_is_retried_instead_of_being_lost(monkeypatch):
    monkeypatch.setattr(recovery, "POLICIES", _policy("local.wren.one"))
    recovery.enqueue(["local.wren.one"], NOW)
    monkeypatch.setattr(recovery, "launch_status", lambda label: {"running": False, "exit_code": 0})
    monkeypatch.setattr(recovery, "start", lambda label: False)

    assert recovery.run_once(NOW) == {"status": "start_failed", "label": "local.wren.one"}
    queued = recovery.status()["queued"]
    assert queued[0]["attempts"] == 1
    assert queued[0]["not_before"] == (NOW + timedelta(seconds=60)).isoformat(timespec="seconds")


def test_recovery_context_records_task_failure(monkeypatch):
    monkeypatch.setattr(recovery, "POLICIES", _policy("local.wren.one", "one"))
    recovery.enqueue(["local.wren.one"], NOW)
    monkeypatch.setattr(recovery, "launch_status", lambda label: {"running": False, "exit_code": 0})
    monkeypatch.setattr(recovery, "start", lambda label: True)
    recovery.run_once(NOW)

    assert recovery.recovering_task("one", "Ollama is down") is True
    assert recovery.recovering_task("different", "boom") is False
    assert recovery.status()["active"]["failures"] == ["Ollama is down"]


def test_policies_cover_every_committed_calendar_job():
    root = Path(__file__).resolve().parent.parent
    labels = set()
    for path in root.glob("launchd/*.plist"):
        with path.open("rb") as fh:
            data = plistlib.load(fh)
        if data.get("StartCalendarInterval"):
            labels.add(data["Label"])
    assert set(recovery.POLICIES) == labels


def test_healer_queues_reactive_work_instead_of_kickstarting_it():
    root = Path(__file__).resolve().parent.parent
    source = (root / "launchd" / "reload-after-upgrade.sh").read_text()
    assert "tasks.startup_recovery" in source
    assert "--enqueue \"${queued[@]}\"" in source
    assert 'launchctl kickstart "$DOMAIN/$label"' not in source


def _main_log_lines(monkeypatch, result):
    """Run main() with run_once stubbed and return the lines it logged."""
    import logging

    monkeypatch.setattr(recovery, "run_once", lambda *a, **k: result)
    lines = []

    class _Capture(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    logger = logging.getLogger("startup_recovery_test_capture")
    logger.handlers = [_Capture()]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    monkeypatch.setattr(recovery, "setup_logger", lambda name: logger)

    assert recovery.main([]) == 0
    return lines


def test_an_idle_poll_logs_nothing(monkeypatch):
    """This fires every 60s and is idle essentially always — 5,474 of 5,474
    polls when measured. Three unconditional lines per poll is 4,320 a day, and
    half of them land in the .launchd.log mirror that nothing rotates: 1.0 MB in
    four days, against the ~19 KB/day chat/logview.py states as expected."""
    assert _main_log_lines(monkeypatch, {"status": "idle"}) == []


def test_a_poll_that_did_something_still_says_so(monkeypatch):
    """The other half. Silencing the idle case must not silence the recovery
    itself — that line is the only record a reboot catch-up ever fired."""
    lines = _main_log_lines(monkeypatch, {"status": "started", "label": "local.wren.one"})

    assert len(lines) == 1
    assert "local.wren.one" in lines[0]


def test_no_run_boundary_lines_are_written(monkeypatch):
    """startup_recovery is a daemon by chat/insights.py's rule (StartInterval,
    no StartCalendarInterval), so parse_runs skips its log and AGENTS.md says to
    leave the boundaries out. A future edit that adds them back would put 2,880
    dead lines a day into an unrotated file."""
    for result in ({"status": "idle"}, {"status": "started", "label": "x"}):
        for line in _main_log_lines(monkeypatch, result):
            assert "run complete" not in line.lower(), line
            assert not line.lower().startswith("starting "), line
