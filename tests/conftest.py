"""Shared pytest fixtures.

Task-runner tests exercise `main()` (strava_download, bg_worker, opportunity_digest,
reminder_sweep, daily_chrome_learnings, daily_youtube_learnings), and `main()`
calls `setup_logger`, which writes to `tasks._common.LOGS_DIR` — the real `logs/`
directory. Left alone, every run appends fixture rows (e.g. the strava_download tests'
"Morning Run" on 2026-07-08) into the production logs. Redirect LOGS_DIR to a tmp
dir for every test so the suite can never pollute real logs.

A fixture alone is not enough, though. `setup_logger` resolves LOGS_DIR into an
absolute path and hands it to a RotatingFileHandler, so redirecting LOGS_DIR only
affects *later* calls. `chat/server.py` calls `setup_logger("wren")` at module
level, which runs when a test module imports it — during collection, before any
fixture has run — so the handler was already pinned to the real logs/wren.log and
the autouse fixture below quietly did nothing. 594 fixture rows (test_server's
"TinyCo" opportunities) reached the production log that way. Hence the module-level
redirect: conftest is fully imported before any test module, so reassigning LOGS_DIR
here lands ahead of every import-time setup_logger call — server.py's and any future
module's.

A monkeypatch also stops at the process boundary: test_bg_worker's idle-poll test
spawns a real child interpreter that runs `bg_worker.main()`, and that child got the
real logs/ no matter what the parent patched. So the redirect goes through the
WREN_LOGS_DIR env var too, which children inherit. test_conftest.py guards both —
that no handler in-process escapes to the real logs/, and that a child doesn't.

A redirect alone is still only as good as its own presence, though, and its
absence is silent. On 2026-07-14, five minutes *after* the redirect below landed,
a 36-line `pytest tests/test_server.py` run appended straight into the production
logs/wren.log — the throttle's RFC 5737 IPs, "model exploded", the TinyCo
opportunity rows. Nothing failed; the rows just showed up, and were only noticed
two days later when the log inspector started classifying every [ERROR] line as an
overnight failure. Removing the three redirect lines reproduces that block exactly,
which is the whole problem: the guard protects the suite only while it is in
effect, and the moment it isn't — deleted, reordered behind a new import-time
setup_logger, or bypassed by a module that resolves logs/ on its own — the failure
mode is silent pollution rather than a red test. So the redirect is backed by a
hard block: `_forbid_production_log_handlers` makes opening any log file in the
real logs/ raise at handler construction. A missed redirect then fails loudly, in
the test that caused it, instead of being discovered in the log two days later.

`chat/insights.py` is why the block covers more than `_common`: it resolves its own
`LOGS_DIR = _ROOT / "logs"`, independently of `_common.LOGS_DIR` and of the env var,
and `run_task_now` opens `<task>.launchd.log` there for append before spawning the
real task module. It was never redirected — the exact "a module that resolves logs/
on its own" case. It is redirected below now, and the block is what would have made
that gap audible.

The learnings tasks write reviews to `LEARNINGS_DIR` — Craig's Obsidian vault
under ~/Documents. Tests stub the writer per-test, but redirect LEARNINGS_DIR to
tmp_path suite-wide as the backstop, so a missed stub lands a fixture file in a
throwaway dir, never in the real vault.

Those same `main()` calls also reach `notify_failure` on a failure path (e.g.
strava_download's partial-failure alert), which POSTs to the real ntfy server when
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

Every other JSON store under config/ gets that same backstop, for the same
reason (see _isolate_remaining_config_stores): wren_memory.json, bg_jobs.json,
reminders.json, github_starred_state.json, and the WIKI_VAULT_PATH vault. All
were per-test-redirected only — the pre-incident position opportunities.json
was in. Adding a new store means adding it there in the same commit; the
per-test monkeypatch stays the convention, this is what makes missing it
harmless. (agent/prefs.py is deliberately absent: it is read-only at import.)

The cloud LLM backend is a network egress like ntfy: a test that selects
WREN_LLM_BACKEND=gemini (or forgets to stub it) must never reach Google.
`loop._gemini_client` is the single client-construction choke point, so blanket-
stub it to raise; test_loop's Gemini tests re-patch it per-test with a fake
client to exercise the real adapter without a network call.
"""

import logging
import os
import tempfile
from pathlib import Path

import pytest

from agent import loop as _loop
from agent.tools import background as _background
from agent.tools import email as _email
from agent.tools import memory as _memory
from agent.tools import notify as _notify
from agent.tools import opportunities as _opportunities
from agent.tools import reminders as _reminders
from chat import insights as _insights
from tasks import _chat_transcripts as _chat_transcripts
from tasks import _common
from tasks import ai_chat_learnings as _ai_chat_learnings
from tasks import morning_brief as _morning_brief
from tasks import opportunity_digest as _opportunity_digest

# Resolved from the source tree rather than from any redirect, so it still names
# the real directory when a redirect is the thing that's broken.
_REAL_LOGS_DIR = Path(_common.__file__).resolve().parent.parent / "logs"

# Both lines run at conftest import — before any test module imports a module that
# calls setup_logger at import time. See the module docstring. The env var covers
# child interpreters (test_bg_worker spawns one, and it ran the real main()); the
# attribute covers this process, where _common was imported before the env was set.
_TEST_LOGS_DIR = Path(tempfile.mkdtemp(prefix="wren-test-logs-"))
os.environ["WREN_LOGS_DIR"] = str(_TEST_LOGS_DIR)
_common.LOGS_DIR = _TEST_LOGS_DIR


def _forbid_production_log_handlers() -> None:
    """Make a log handler on the real logs/ raise instead of quietly appending.

    The backstop behind every logs/ redirect above (see the module docstring):
    those move the path, this refuses the write. Patched onto FileHandler, which
    RotatingFileHandler — what setup_logger actually builds — constructs through.

    Installed at import, permanently and process-wide, for the same reason the
    redirect is: setup_logger runs at *import* time in chat/server.py, so a
    fixture is already too late to see it. Nothing legitimately writes into the
    real logs/ during a test run, so there is nothing to let through.
    """
    original_init = logging.FileHandler.__init__

    def _guarded_init(self, filename, *args, **kwargs):
        if Path(filename).resolve().parent == _REAL_LOGS_DIR:
            raise RuntimeError(
                f"a test tried to open the production log {filename} — the logs/ "
                "redirect in tests/conftest.py is not in effect for whatever built "
                "this handler. Fixture rows would have gone into Craig's real logs "
                "(and the 8am log inspector would report them as overnight failures)."
            )
        return original_init(self, filename, *args, **kwargs)

    logging.FileHandler.__init__ = _guarded_init


_forbid_production_log_handlers()


@pytest.fixture(autouse=True)
def _isolate_task_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(_common, "LOGS_DIR", tmp_path)
    # insights resolves logs/ itself and opens <task>.launchd.log there for append
    # (run_task_now), so _common's redirect never covered it. Reads resolve it at
    # call time, so the fixture is enough — there is no import-time binding here.
    monkeypatch.setattr(_insights, "LOGS_DIR", tmp_path)


@pytest.fixture(autouse=True)
def _isolate_learnings_dir(tmp_path, monkeypatch):
    # learnings_file._learnings_dir() reads this env at call time.
    monkeypatch.setenv("LEARNINGS_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _isolate_ai_chat_learnings(tmp_path, monkeypatch):
    # Redirect the Gemini-dedup store to tmp, and point both chat sources away
    # from Craig's real data: no test may read ~/.claude session transcripts or
    # the real Gemini drop folder, and none may write the production state store.
    monkeypatch.setattr(_ai_chat_learnings, "STATE_PATH",
                        tmp_path / "ai_chat_learnings_state.json")
    monkeypatch.setattr(_chat_transcripts, "CLAUDE_PROJECTS_DIR", tmp_path / "claude_projects")
    monkeypatch.setenv("WREN_GEMINI_CHATS_DIR", str(tmp_path / "gemini_inbox"))


@pytest.fixture(autouse=True)
def _isolate_opportunity_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(_opportunities, "_STORE_PATH", tmp_path / "opportunities.json")
    monkeypatch.setattr(_opportunity_digest, "STATE_PATH",
                        tmp_path / "opportunities_state.json")


@pytest.fixture(autouse=True)
def _isolate_remaining_config_stores(tmp_path, monkeypatch):
    # The rest of the JSON stores under config/, given the same blanket backstop
    # as opportunities.json above and for the same reason — each is redirected
    # per-test today, which is exactly the position opportunities.json was in
    # when a surviving thread wrote fixture data over the production file. The
    # stakes here are higher than a stale digest: wren_memory.json holds pinned
    # facts injected into every future system prompt, and bg_jobs.json/
    # reminders.json drive real side effects (re-run jobs, duplicate pushes).
    #
    # Each path is deliberately NOT created: a missing file is the stores' empty
    # state, so an unstubbed read degrades to "no data" rather than inheriting
    # whatever a previous test wrote.
    monkeypatch.setattr(_memory, "_STORE_PATH", tmp_path / "wren_memory.json")
    monkeypatch.setattr(_background, "_STORE_PATH", tmp_path / "bg_jobs.json")
    monkeypatch.setattr(_reminders, "_STORE_PATH", tmp_path / "reminders.json")
    monkeypatch.setattr(_morning_brief, "STARRED_STATE_PATH",
                        tmp_path / "github_starred_state.json")
    # wiki.py resolves this env on every _vault() call. Craig's real vault is a
    # readable path on this machine, so without the redirect a wiki test that
    # forgets to stub reads his actual notes into a fixture assertion.
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path / "wiki_vault"))


class _StubNtfyResponse:
    def raise_for_status(self):
        pass


@pytest.fixture(autouse=True)
def _block_ntfy_push(monkeypatch):
    monkeypatch.setattr(_notify.requests, "post", lambda *a, **k: _StubNtfyResponse())


@pytest.fixture(autouse=True)
def _block_email_send(monkeypatch):
    # notify(email_fallback=True) sends a real Gmail message when a push fails,
    # which put an egress path behind the *failure* branch of a function whose
    # error paths tests exercise on purpose (test_notify.py). Without this, those
    # tests mail Craig every run. Patched on the module so notify's call-time
    # lookup resolves the stub; callers that bound `send_email` at import (e.g.
    # morning_brief) still stub their own, as they always have.
    def _no_real_email(*a, **k):
        raise RuntimeError("real Gmail send blocked in tests — stub send_email")
    monkeypatch.setattr(_email, "send_email", _no_real_email)


@pytest.fixture(autouse=True)
def _block_gemini_client(monkeypatch):
    def _no_real_gemini(*a, **k):
        raise RuntimeError("real Gemini client blocked in tests — stub loop._gemini_client")
    monkeypatch.setattr(_loop, "_gemini_client", _no_real_gemini)
