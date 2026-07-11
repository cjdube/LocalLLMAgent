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

The opportunities store gets the same blanket protection as the logs: tests
isolate it by monkeypatching `opportunities._STORE_PATH`, but a research
thread spawned by a server test once outlived its test, raced monkeypatch
teardown mid-write, and saved its tmp-store fixture data over the production
config/opportunities.json. Pointing the store (and the digest watermark) at
tmp_path for every test makes that class of miss land in a throwaway file,
never in config/.
"""

import pytest

from agent.tools import notify as _notify
from agent.tools import opportunities as _opportunities
from tasks import _common
from tasks import opportunity_digest as _opportunity_digest


@pytest.fixture(autouse=True)
def _isolate_task_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(_common, "LOGS_DIR", tmp_path)


@pytest.fixture(autouse=True)
def _isolate_opportunity_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(_opportunities, "_STORE_PATH", tmp_path / "opportunities.json")
    monkeypatch.setattr(_opportunity_digest, "STATE_PATH",
                        tmp_path / "opportunities_state.json")


class _StubNtfyResponse:
    def raise_for_status(self):
        pass


@pytest.fixture(autouse=True)
def _block_ntfy_push(monkeypatch):
    monkeypatch.setattr(_notify.requests, "post", lambda *a, **k: _StubNtfyResponse())
