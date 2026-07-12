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

The cloud LLM backend is a network egress like ntfy: a test that selects
WREN_LLM_BACKEND=gemini (or forgets to stub it) must never reach Google.
`loop._gemini_client` is the single client-construction choke point, so blanket-
stub it to raise; test_loop's Gemini tests re-patch it per-test with a fake
client to exercise the real adapter without a network call.
"""

import pytest

from agent import loop as _loop
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


@pytest.fixture(autouse=True)
def _block_gemini_client(monkeypatch):
    def _no_real_gemini(*a, **k):
        raise RuntimeError("real Gemini client blocked in tests — stub loop._gemini_client")
    monkeypatch.setattr(_loop, "_gemini_client", _no_real_gemini)
