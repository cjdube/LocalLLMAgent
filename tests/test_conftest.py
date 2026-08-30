"""Guards on the suite-wide isolation in conftest.py.

conftest redirects `tasks._common.LOGS_DIR` at import time so that modules which
call `setup_logger` at module level (chat/server.py) bind their RotatingFileHandler
to a tmp dir instead of the real logs/, and sets WREN_LOGS_DIR so child interpreters
spawned by tests inherit the same redirect. Both are invisible — nothing fails if
they regress, the suite just quietly starts appending fixture rows to the production
log again, as it did for 594 lines. Assert them directly instead.
"""

import logging
import os
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from chat import insights
from tasks import _common

os.environ.setdefault("WREN_CHAT_TOKEN", "test-token")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import chat.server  # noqa: F401  — imported for its module-level setup_logger("wren")

# Derived from the source tree, not from conftest, so this can't agree with a
# broken redirect by sharing its mistake.
_REAL_LOGS_DIR = Path(_common.__file__).resolve().parent.parent / "logs"


def _file_handlers():
    loggers = [logging.getLogger()]
    loggers += [
        lg
        for lg in logging.Logger.manager.loggerDict.values()
        if isinstance(lg, logging.Logger)
    ]
    return [
        (lg.name, h)
        for lg in loggers
        for h in lg.handlers
        if isinstance(h, logging.FileHandler)
    ]


def test_logs_dir_is_redirected_away_from_production():
    assert _common.LOGS_DIR != _REAL_LOGS_DIR


def test_no_log_handler_writes_into_the_real_logs_dir():
    escaped = [
        f"{name} -> {h.baseFilename}"
        for name, h in _file_handlers()
        if Path(h.baseFilename).resolve().parent == _REAL_LOGS_DIR
    ]
    assert not escaped, (
        "log handlers are bound to the production logs/ dir: "
        + "; ".join(escaped)
        + " — these append fixture rows to real logs on every pytest run"
    )


def test_a_child_interpreter_inherits_the_logs_redirect():
    # test_bg_worker spawns a real child that runs bg_worker.main(); a parent-side
    # monkeypatch cannot reach it, so the redirect has to travel via the environment.
    code = "\n".join([
        "from tasks._common import setup_logger",
        "lg = setup_logger('conftest_guard_probe')",
        "print(lg.handlers[0].baseFilename)",
    ])
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    child_log = Path(proc.stdout.strip().splitlines()[-1]).resolve()
    assert child_log.parent != _REAL_LOGS_DIR, (
        f"a child interpreter wrote {child_log} — WREN_LOGS_DIR is not reaching subprocesses"
    )


def test_every_config_store_is_redirected_away_from_production():
    # Same shape as the logs guard, for the JSON stores: the redirect is
    # invisible, and a regression doesn't fail anything — it just quietly starts
    # writing fixture data into config/, which is how fixture opportunities once
    # reached the production store. Resolve the real dir from the source tree so
    # this can't agree with a broken redirect by sharing its mistake.
    from agent.tools import background, memory, opportunities, reminders
    from tasks import morning_brief, opportunity_digest

    real_config = Path(_common.__file__).resolve().parent.parent / "config"
    stores = {
        "memory._STORE_PATH": memory._STORE_PATH,
        "background._STORE_PATH": background._STORE_PATH,
        "reminders._STORE_PATH": reminders._STORE_PATH,
        "opportunities._STORE_PATH": opportunities._STORE_PATH,
        "opportunity_digest.STATE_PATH": opportunity_digest.STATE_PATH,
        "morning_brief.STARRED_STATE_PATH": morning_brief.STARRED_STATE_PATH,
    }
    escaped = [f"{name} -> {p}" for name, p in stores.items()
               if Path(p).resolve().parent == real_config]
    assert not escaped, (
        "these stores still point into the production config/ dir: "
        + "; ".join(escaped)
        + " — a test writing one clobbers real data"
    )


def test_wiki_vault_is_redirected_away_from_the_real_vault():
    from agent.tools import wiki

    vault = wiki._vault()
    assert vault != Path(wiki.DEFAULT_WIKI_VAULT).expanduser(), \
        "WIKI_VAULT_PATH still resolves to the user's real Obsidian vault"


def test_a_handler_on_the_real_logs_dir_is_refused():
    # The backstop behind the redirects: they move the path, this refuses the
    # write. Without it, a redirect's absence is silent — which is how a
    # test_server run appended 36 fixture rows to the production wren.log on
    # 2026-07-14, five minutes after the redirect landed. Build the handler
    # setup_logger builds, at the path it would have used.
    with pytest.raises(RuntimeError, match="production log"):
        RotatingFileHandler(_REAL_LOGS_DIR / "wren.log", maxBytes=2_000_000, backupCount=3)


def test_the_refusal_covers_plain_file_handlers_too():
    # Patched onto FileHandler rather than RotatingFileHandler so a future module
    # that reaches for logging's plain handler is covered without a code change.
    with pytest.raises(RuntimeError, match="production log"):
        logging.FileHandler(_REAL_LOGS_DIR / "anything.log")


def test_the_refusal_leaves_handlers_outside_the_real_logs_dir_alone(tmp_path):
    # The block must be narrow: every task logger in the suite builds a handler
    # under tmp_path, and they all have to keep working.
    handler = RotatingFileHandler(tmp_path / "wren.log")
    handler.close()


def test_insights_logs_dir_is_redirected_away_from_production():
    # insights resolves logs/ on its own — not via _common.LOGS_DIR or WREN_LOGS_DIR
    # — and run_task_now opens <task>.launchd.log there for append before spawning
    # the real task module. It was the one logs/ path with no redirect at all.
    assert Path(insights.LOGS_DIR).resolve() != _REAL_LOGS_DIR


def test_insights_launchd_dir_is_redirected_away_from_the_repo():
    # The other half of task discovery. insights globs LAUNCHD_DIR for plists, so
    # unpinned, the task list a test sees is whatever this checkout has installed
    # — /api/schedules, /api/capabilities, system_map and /api/logs all reach it
    # without naming a task. Nothing writes, so the risk is a machine-dependent
    # assertion rather than production damage, which is also why it would go
    # unnoticed: it fails only on someone else's checkout.
    real_launchd = Path(insights.__file__).resolve().parent.parent / "launchd"
    assert Path(insights.LAUNCHD_DIR).resolve() != real_launchd
    assert not list(Path(insights.LAUNCHD_DIR).glob("*.plist"))


def test_insights_launch_agents_is_redirected_away_from_the_real_one():
    # The third plist source, and the worst of the three unpinned: an external
    # root is searched in ~/Library/LaunchAgents by label prefix, so the task
    # list would depend on which agents this USER has installed, not just which
    # plists this checkout carries.
    real = Path("~/Library/LaunchAgents").expanduser()
    assert Path(insights.LAUNCH_AGENTS).resolve() != real.resolve()
    assert not list(Path(insights.LAUNCH_AGENTS).glob("*.plist"))


def test_scribejay_config_is_redirected_away_from_the_users_own():
    # /map's ScribeJay label reads ~/.scribejay/config.json. Read-only, so this
    # is determinism, not damage — but an assertion that passes because this
    # machine happens to run ollama fails on a machine that doesn't.
    real = Path("~/.scribejay/config.json").expanduser()
    assert Path(insights.SCRIBEJAY_CONFIG).resolve() != real.resolve()
    assert insights._scribejay_backend() == "ollama"


def test_task_discovery_cache_does_not_survive_between_tests():
    # discover_tasks() caches on a signature of (plist name, mtime) — NOT on the
    # directory — so an entry built under one test's tmp dir can be served to the
    # next test whose dir hashes the same way, which two empty dirs do. The
    # redirect above is only as good as this clearing, and a stale hit would look
    # like a passing test reading another test's fixtures.
    assert insights._TASKS_CACHE == {}
    insights.discover_tasks()
    assert insights._TASKS_CACHE != {}, "expected discover_tasks to populate its cache"


def test_the_wren_logger_server_binds_at_import_is_covered():
    # chat/server.py's module-level setup_logger("wren") is the specific call that
    # defeated the autouse fixture; pin it so the guard has teeth even if the
    # broader sweep above stops finding handlers for some unrelated reason.
    handlers = [h for h in logging.getLogger("wren").handlers
                if isinstance(h, logging.FileHandler)]
    assert handlers, "expected chat.server to have bound a file handler on 'wren'"
    for h in handlers:
        assert Path(h.baseFilename).resolve().parent != _REAL_LOGS_DIR


def test_ntfy_egress_is_stubbed_for_both_verbs():
    # notify() POSTs a push at the user's phone; ntfy_health() GETs the live server
    # — and load_env()s the REAL config/.env to find its URL, so nothing a test
    # sets in the environment keeps it local. Both verbs are guarded suite-wide;
    # only post was, until the dashboard's health pill added the second one.
    # Same shape as the logs guard: a regression here is silent (a real push, a
    # real probe), so assert it directly rather than trusting the fixture's
    # presence.
    from agent.tools import notify as notify_mod

    for verb in ("post", "get"):
        fn = getattr(notify_mod.requests, verb)
        assert fn.__module__ != "requests.api", (
            f"agent.tools.notify.requests.{verb} is the real requests function — "
            "conftest's _block_ntfy_egress is not in effect, and the suite can "
            "reach the user's actual ntfy server"
        )
