"""Tests for tasks/_common.py — the shared task-runner helpers (logger setup,
date string, best-effort failure push). Log output is redirected to a tmp dir
by conftest; the ntfy push is stubbed there too, and re-stubbed per-test here
to observe what notify_failure sends."""

import logging
from datetime import datetime

from tasks import _common


def test_today_str_is_iso_date():
    assert _common.today_str() == datetime.now().strftime("%Y-%m-%d")


def test_setup_logger_writes_to_the_redirected_dir():
    # conftest points _common.LOGS_DIR at a tmp dir for every test; assert
    # setup_logger writes there (and never the real logs/, which conftest's
    # handler guard would otherwise reject).
    logger = _common.setup_logger("unittest_task")
    logger.info("hello world")
    for handler in logger.handlers:
        handler.flush()
    log_file = _common.LOGS_DIR / "unittest_task.log"
    assert log_file.exists()
    assert "hello world" in log_file.read_text()


def test_setup_logger_is_isolated_and_does_not_stack_handlers():
    logger = _common.setup_logger("iso_task")
    assert logger.propagate is False
    # A second setup for the same name clears old handlers rather than doubling
    # them (file + stream), so re-running a task can't multiply its log lines.
    again = _common.setup_logger("iso_task")
    assert len(again.handlers) == 2


def test_notify_failure_pushes_high_priority_with_email_fallback(monkeypatch):
    calls = {}

    def fake_notify(message, title=None, priority=None, email_fallback=False):
        calls.update(message=message, title=title, priority=priority,
                     email_fallback=email_fallback)
        return {"ok": True}
    monkeypatch.setattr(_common, "notify", fake_notify)
    _common.notify_failure("morning_brief", "boom")
    assert calls["email_fallback"] is True     # a one-shot alert must not be lost
    assert calls["priority"] == "high"
    assert "morning_brief" in calls["title"]
    assert "boom" in calls["message"]


def test_notify_failure_defers_alert_during_startup_recovery(monkeypatch):
    from tasks import startup_recovery
    monkeypatch.setattr(startup_recovery, "recovering_task", lambda task, detail: task == "morning_brief")
    monkeypatch.setattr(_common, "notify", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not push")))

    _common.notify_failure("morning_brief", "Ollama down")


def test_notify_failure_swallows_push_errors(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("push exploded")
    monkeypatch.setattr(_common, "notify", boom)
    # Must not raise — the task failure it's reporting is already logged.
    _common.notify_failure("t", "detail", logger=logging.getLogger("test_common"))


def test_notify_failure_logs_when_push_reports_an_error(monkeypatch):
    monkeypatch.setattr(_common, "notify", lambda *a, **k: {"error": "ntfy down"})
    warnings = []

    class _Logger:
        def warning(self, msg, *args):
            warnings.append(msg % args if args else msg)

        def exception(self, *a, **k):
            pass

    _common.notify_failure("t", "d", logger=_Logger())
    assert any("did not send" in w for w in warnings)
