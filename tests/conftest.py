"""Shared pytest fixtures.

Task-runner tests exercise `main()` (daily_log, bg_worker, opportunity_digest,
reminder_sweep, weekly_learnings), and `main()` calls `setup_logger`, which
writes to `tasks._common.LOGS_DIR` — the real `logs/` directory. Left alone,
every run appends fixture rows (e.g. the daily_log tests' "Morning Run" on
2026-07-08) into the production logs. Redirect LOGS_DIR to a tmp dir for every
test so the suite can never pollute real logs.

Those same `main()` calls also reach `notify_failure` on a failure path (e.g.
daily_log's partial-failure alert), which POSTs to the real ntfy server when
NTFY_URL is configured — firing an actual push to Craig's phone every test run.
Stub the single push egress (agent.tools.notify.requests.post) for every test
so the suite can never send a real alert; test_notify.py re-patches it per-test
to exercise the real code.
"""

import pytest

from agent.tools import notify as _notify
from tasks import _common


@pytest.fixture(autouse=True)
def _isolate_task_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(_common, "LOGS_DIR", tmp_path)


class _StubNtfyResponse:
    def raise_for_status(self):
        pass


@pytest.fixture(autouse=True)
def _block_ntfy_push(monkeypatch):
    monkeypatch.setattr(_notify.requests, "post", lambda *a, **k: _StubNtfyResponse())
