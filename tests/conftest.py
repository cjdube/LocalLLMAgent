"""Shared pytest fixtures.

Task-runner tests exercise `main()` (daily_log, bg_worker, opportunity_digest,
reminder_sweep, weekly_learnings), and `main()` calls `setup_logger`, which
writes to `tasks._common.LOGS_DIR` — the real `logs/` directory. Left alone,
every run appends fixture rows (e.g. the daily_log tests' "Morning Run" on
2026-07-08) into the production logs. Redirect LOGS_DIR to a tmp dir for every
test so the suite can never pollute real logs.
"""

import pytest

from tasks import _common


@pytest.fixture(autouse=True)
def _isolate_task_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(_common, "LOGS_DIR", tmp_path)
