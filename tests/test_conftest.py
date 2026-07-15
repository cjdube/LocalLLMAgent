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
from pathlib import Path

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
        "WIKI_VAULT_PATH still resolves to Craig's real Obsidian vault"


def test_the_wren_logger_server_binds_at_import_is_covered():
    # chat/server.py's module-level setup_logger("wren") is the specific call that
    # defeated the autouse fixture; pin it so the guard has teeth even if the
    # broader sweep above stops finding handlers for some unrelated reason.
    handlers = [h for h in logging.getLogger("wren").handlers
                if isinstance(h, logging.FileHandler)]
    assert handlers, "expected chat.server to have bound a file handler on 'wren'"
    for h in handlers:
        assert Path(h.baseFilename).resolve().parent != _REAL_LOGS_DIR
