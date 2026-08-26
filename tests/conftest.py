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

That module has two more ambient inputs, both redirected below for determinism
rather than for safety: `LAUNCHD_DIR` (it globs the repo's real `launchd/`, so
the task list a test sees depends on which plists this checkout has installed)
and `WREN_EXTERNAL_TASK_ROOTS` (it reaches into whatever sibling repos that
names). Neither writes anything; both make an assertion depend on the machine.

The learnings tasks write reviews to `LEARNINGS_DIR` — the user's Obsidian vault
under ~/Vaults. Tests stub the writer per-test, but redirect LEARNINGS_DIR to
tmp_path suite-wide as the backstop, so a missed stub lands a fixture file in a
throwaway dir, never in the real vault.

Those same `main()` calls also reach `notify_failure` on a failure path (e.g.
strava_download's partial-failure alert), which POSTs to the real ntfy server when
NTFY_URL is configured — firing an actual push to the user's phone every test run.
Stub the push egress (agent.tools.notify.requests.post) for every test so the suite
can never send a real alert; test_notify.py re-patches it per-test to exercise the
real code. `requests.get` is stubbed alongside it: ntfy_health() probes the live
server, and it load_env()s the real config/.env to find it, so the dashboard's
health endpoint would otherwise reach the real box from a server test.

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

The games registry (agent/tools/games.py) is stubbed for a different reason than
the stores above: it writes nothing, but it *reads* the machine — a loopback
socket probe and a checkout under ~/Projects — so `available` would depend on
whether the developer happens to have the game's dev server running. Both are
pinned suite-wide so the answer is the same everywhere.

The project scanner (agent/tools/projects.py) gets the games treatment for the
games reason: it reads the machine rather than writing it. Unpinned it walks the
developer's real ~/Projects and shells out to git for every checkout there, so
assertions would depend on which repos they happen to have cloned. PROJECTS_DIR
is pinned at an empty tmp dir suite-wide; the registry it feeds
(config/projects.json, via tasks/project_scan.py) is a store like any other and
is redirected with the rest.

The cloud LLM backend is a network egress like ntfy: a test that selects
WREN_LLM_BACKEND=gemini (or forgets to stub it) must never reach Google.
`agent.backends.gemini._gemini_client` is the single client-construction choke
point (the backend adapter _gemini_chat resolves it there), so blanket-stub it
to raise; test_loop's Gemini tests re-patch it per-test with a fake client to
exercise the real adapter without a network call.
"""

import logging
import os
import tempfile
from pathlib import Path

import pytest

from agent import escalations as _escalations
from agent import loop as _loop
from agent.backends import gemini as _gemini_backend
from agent.tools import background as _background
from agent.tools import email as _email
from agent.tools import games as _games
from agent.tools import mail_state as _mail_state
from agent.tools import memory as _memory
from agent.tools import notify as _notify
from agent.tools import opportunities as _opportunities
from agent.tools import projects as _projects_tool
from agent.tools import push_log as _push_log
from agent.tools import reminders as _reminders
from chat import insights as _insights
from chat import wikilint as _wikilint
from evals import run_eval as _run_eval
from scribejay import transcripts as _chat_transcripts
from tasks import _common
from scribejay import ai_chat_learnings as _ai_chat_learnings
from tasks import mail_watcher as _mail_watcher
from tasks import morning_brief as _morning_brief
from tasks import opportunity_digest as _opportunity_digest

from tasks import starred_blurbs as _starred_blurbs
from tasks import starred_installed as _starred_installed
from tasks import starred_releases as _starred_releases

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
                "this handler. Fixture rows would have gone into the user's real logs "
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
def _isolate_launchd_dir(tmp_path, monkeypatch):
    """Pin task discovery at an empty plist dir — the games/projects treatment,
    for the games/projects reason: `chat/insights.py` *reads the machine*.

    `LAUNCHD_DIR` is the last of insights' three ambient inputs to get a
    backstop. `LOGS_DIR` is redirected above and `WREN_EXTERNAL_TASK_ROOTS` is
    unset below, but discover_tasks() also globs the repo's real `launchd/`, so
    which tasks a test sees depends on which plists are installed in this
    checkout — /api/schedules, /api/capabilities, system_map and /api/logs all
    reach it without asking for any particular task. Nothing here writes, so
    this is about determinism rather than protecting production state: the log
    paths those plists produce already resolve under the redirected LOGS_DIR.

    _TASKS_CACHE has to be cleared with it. discover_tasks() caches on a
    signature of (plist name, mtime), NOT on the directory — so an entry built
    under one test's dir can be served to the next test whose dir happens to
    hash the same way, which is exactly what two empty dirs do. Clearing on both
    sides makes the redirect mean what it says. _RUNS_CACHE is keyed on the full
    log path and so can't collide, but it costs nothing to clear alongside.

    Per-test redirects stay the convention (test_insights.py and
    test_logview.py write their own fixture plists); this is what makes missing
    one harmless.
    """
    launchd = tmp_path / "launchd"
    launchd.mkdir(exist_ok=True)
    monkeypatch.setattr(_insights, "LAUNCHD_DIR", launchd)
    _insights._TASKS_CACHE.clear()
    _insights._RUNS_CACHE.clear()
    yield
    _insights._TASKS_CACHE.clear()
    _insights._RUNS_CACHE.clear()


@pytest.fixture(autouse=True)
def _no_external_task_roots(monkeypatch):
    # discover_tasks() reaches into whatever sibling repos this env var names,
    # reading their launchd/ and logs/. Unset, so the suite never depends on
    # which checkouts happen to exist on this machine and never parses a real
    # repo's production logs into a test assertion. Reads are at call time, so
    # a test that wants external roots sets it back itself.
    monkeypatch.delenv("WREN_EXTERNAL_TASK_ROOTS", raising=False)


@pytest.fixture(autouse=True)
def _isolate_learnings_dir(tmp_path, monkeypatch):
    # learnings_file._learnings_dir() reads this env at call time.
    monkeypatch.setenv("LEARNINGS_DIR", str(tmp_path))
    # daily_synthesis archives its nudges outside the ingest queue, in its own
    # vault dir — a second real path under ~/Vaults, so it gets the same
    # backstop (daily_synthesis._synthesis_dir() also reads it at call time).
    monkeypatch.setenv("SYNTHESIS_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _isolate_skills_dir(tmp_path, monkeypatch):
    # skills/ is a TRACKED repo directory, not a gitignored config store, so a
    # test that writes a fixture skill doesn't just dirty local state — it stages
    # a file for commit. tests/test_skills.py has its own fixture; this is the
    # backstop for everything else that reaches the skills tools without one
    # (bg_worker's toolset, insights.system_map(), the chat system prompt's
    # render_skills_index()), all of which otherwise READ the real skills/ dir and
    # inherit whatever procedures happen to be saved on this machine.
    # skills._skills_dir() reads this env on every call, so the env var is enough.
    monkeypatch.setenv("WREN_SKILLS_DIR", str(tmp_path / "skills"))


@pytest.fixture(autouse=True)
def _isolate_ai_chat_learnings(tmp_path, monkeypatch):
    # Redirect the Gemini-dedup store to tmp, and point both chat sources away
    # from the user's real data: no test may read ~/.claude session transcripts or
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
    # The delivered-push log. This redirect is load-bearing, not a backstop:
    # _block_ntfy_egress below stubs requests.post with a response whose
    # raise_for_status() passes, so notify() reaches its SUCCESS branch in every
    # test that pushes — and that branch calls push_log.record(). Without this,
    # the suite writes fixture notifications into the production log, and Wren
    # then reports them in chat as things she sent.
    monkeypatch.setattr(_push_log, "_STORE_PATH", tmp_path / "push_log.json")
    monkeypatch.setattr(_morning_brief, "STARRED_STATE_PATH",
                        tmp_path / "github_starred_state.json")
    # The /starred view's cached blurbs. server.py reads this path off the
    # starred_blurbs module at call time, so this one redirect covers both the
    # task that writes it and the API route that reads it.
    monkeypatch.setattr(_starred_blurbs, "BLURBS_PATH", tmp_path / "starred_blurbs.json")
    # The /starred view's cached latest releases. server.py reads this path off
    # the starred_releases module at call time, so this one redirect covers both
    # the task that writes it and the API route that reads it.
    monkeypatch.setattr(_starred_releases, "RELEASES_PATH", tmp_path / "starred_releases.json")
    # The /starred view's cached repo LIST — the fallback the page renders when
    # the live GitHub fetch fails. Same call-time lookup, so this covers both the
    # task that writes it and chat/routes_starred.py:_repo_list, which reads it.
    monkeypatch.setattr(_starred_releases, "REPOS_PATH", tmp_path / "starred_repos.json")
    # The /starred view's installed-version tracking: the hand-edited source
    # (SOURCE_PATH — the user's real config, which a test must never read) and the
    # task-written resolved cache (INSTALLED_PATH). server.py reads the cache off
    # the module at call time, so redirecting both here covers the task and the
    # API route, and keeps the version-command runner pointed at a throwaway file.
    monkeypatch.setattr(_starred_installed, "SOURCE_PATH", tmp_path / "starred_installed.json")
    monkeypatch.setattr(_starred_installed, "INSTALLED_PATH", tmp_path / "starred_installed_versions.json")
    # The manual frontier-escalation log (chat's "redo with the frontier model").
    # Server-side only, but redirected here for the same reason as the rest: a
    # missed per-test stub lands fixture escalation rows in the real store, never
    # config/escalations.json. See docs/frontier-escalation.md.
    monkeypatch.setattr(_escalations, "_STORE_PATH", tmp_path / "escalations.json")
    # The local project registry. load_registry() resolves this at
    # call time, so this one redirect covers the task that writes it, the chat
    # tools that read it, and daily_synthesis's project anchors.
    monkeypatch.setattr(_projects_tool, "PROJECTS_PATH", tmp_path / "projects.json")
    # The Gmail watcher's watermark, watch expiry and seen-message set. Both the
    # always-on watcher and the daily renewal job write it, and every function in
    # agent/tools/mail_state.py resolves _STORE_PATH at call time, so this one
    # redirect covers the tasks, their tests, and the CLI.
    monkeypatch.setattr(_mail_state, "_STORE_PATH", tmp_path / "mail_state.json")
    # wiki.py resolves this env on every _vault() call. the user's real vault is a
    # readable path on this machine, so without the redirect a wiki test that
    # forgets to stub reads his actual notes into a fixture assertion.
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path / "wiki_vault"))


class _StubNtfyResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {"healthy": True}  # ntfy_health's /v1/health body


@pytest.fixture(autouse=True)
def _block_ntfy_egress(monkeypatch):
    """Stub BOTH verbs the ntfy module speaks — post (publish) and get (health).

    post: notify() fires a real push at the user's phone on every task-failure path
    a test exercises. get: ntfy_health() probes the real ntfy server, and it
    calls load_env() first, so it reads the REAL config/.env — a test hitting
    /api/health/ntfy would reach the live box over the network no matter what
    the test set in the environment.

    The get stub is why this fixture is no longer named _block_ntfy_push: the
    module's egress is two verbs now, and only the post one was ever guarded.
    test_notify.py re-patches both per-test to exercise the real code.
    """
    monkeypatch.setattr(_notify.requests, "post", lambda *a, **k: _StubNtfyResponse())
    monkeypatch.setattr(_notify.requests, "get", lambda *a, **k: _StubNtfyResponse())


@pytest.fixture(autouse=True)
def _block_email_send(monkeypatch):
    # notify(email_fallback=True) sends a real Gmail message when a push fails,
    # which put an egress path behind the *failure* branch of a function whose
    # error paths tests exercise on purpose (test_notify.py). Without this, those
    # tests mail the user every run. Patched on the module so notify's call-time
    # lookup resolves the stub; callers that bound `send_email` at import (e.g.
    # morning_brief) still stub their own, as they always have.
    def _no_real_email(*a, **k):
        raise RuntimeError("real Gmail send blocked in tests — stub send_email")
    monkeypatch.setattr(_email, "send_email", _no_real_email)
    # The other send path. reply_to_thread builds and sends its own message
    # rather than going through send_email, so the guard above never covered
    # it — and this one mails a real third party, not the user.
    monkeypatch.setattr(_email, "reply_to_thread", _no_real_email)


@pytest.fixture(autouse=True)
def _neutralize_escalation_backend(monkeypatch):
    # chat.server load_dotenv()s the real config/.env at import, so a developer who
    # has WREN_ESCALATION_BACKEND set in their .env would otherwise make chat
    # escalation "configured" for every test — flipping escalate_to onto local
    # replies and the /chat/escalate 400. Default it off so the suite doesn't
    # depend on ambient config; test_server's frontier_configured fixture opts
    # back in where it's exercised.
    monkeypatch.delenv("WREN_ESCALATION_BACKEND", raising=False)


@pytest.fixture(autouse=True)
def _block_gemini_client(monkeypatch):
    def _no_real_gemini(*a, **k):
        raise RuntimeError(
            "real Gemini client blocked in tests — stub "
            "agent.backends.gemini._gemini_client")
    monkeypatch.setattr(_gemini_backend, "_gemini_client", _no_real_gemini)


@pytest.fixture(autouse=True)
def _isolate_games(tmp_path, monkeypatch):
    # Two ambient dependencies, same treatment as the stores above.
    #
    # _service_up opens a real socket to a loopback port. That's a live probe of
    # whatever the developer happens to be running, so `available` would flip with
    # the machine's state — a test asserting "unavailable" passes on CI and fails
    # for anyone with the game's dev server up. Stub it off; test_games.py patches
    # it back per-test to exercise both branches.
    #
    # The dist path is the same shape of problem in the other direction: it
    # defaults to a real checkout under ~/Projects, so an assertion about the
    # not-built case depends on the developer not having built it. Point it at an
    # empty tmp dir. Nothing here writes, so this is about determinism rather
    # than protecting production state.
    monkeypatch.setattr(_games, "_service_up", lambda port: False)
    monkeypatch.setenv("WEIGH_ANCHOR_DIR", str(tmp_path / "weigh-anchor"))


@pytest.fixture(autouse=True)
def _isolate_projects_dir(tmp_path, monkeypatch):
    """Pin the project scanner at an empty tmp dir, for the same reason the games
    registry above is stubbed: agent/tools/projects.py *reads the machine*. Left
    alone it walks the developer's real ~/Projects, so a test's result would
    depend on which checkouts they happen to have — and it would shell out to git
    a few dozen times per test while doing it. Nothing here writes, so this is
    about determinism, not protecting production state. _projects_dir() reads the
    env on every call, so the env var is enough; test_projects.py points it at
    its own fixture tree per-test."""
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "projects_dir"))


@pytest.fixture(autouse=True)
def _block_mail_subscriber(monkeypatch):
    """Stop any test from opening a real Pub/Sub streaming pull.

    tasks/mail_watcher.py's _subscribe() is a thread-spawner: the Pub/Sub client
    runs its callback on background threads and blocks the caller on
    future.result(). That is the exact shape of the incident at the top of this
    file — a thread that outlives its test resolves monkeypatched paths after
    teardown, and here those paths are the mail state store. It would also
    authenticate with the user's real Google credentials and reach the network.

    tests/test_mail_watcher.py drives handle_notification() directly, which is
    where the logic lives; nothing legitimately needs the real subscriber.
    """
    def _no_subscriber(*a, **k):
        raise AssertionError(
            "tasks.mail_watcher._subscribe was called in a test — it opens a real "
            "Pub/Sub streaming pull and spawns background threads. Test "
            "handle_notification() instead."
        )

    monkeypatch.setattr(_mail_watcher, "_subscribe", _no_subscriber)


@pytest.fixture(autouse=True)
def _block_wiki_lint_subprocess(monkeypatch):
    """Stop any test from shelling out to the sibling lint repo.

    chat/wikilint.py runs ObsidianWikiAgent's wiki_lint.py as a subprocess, and
    `run_lint(fix=True)` makes that subprocess WRITE to the vault — the only
    write path Wren has into the user's notes. WIKI_VAULT_PATH is redirected
    above, so the writes would land in tmp_path; this is the second lock on the
    same door, because that redirect is an env var the child process inherits
    and a child is exactly what escapes monkeypatch teardown (see the daemon
    thread incident at the top of this file).

    It also keeps the suite honest in the ordinary case: without it, every test
    touching /wiki/lint would spawn a real interpreter from a sibling checkout,
    so the result would depend on whether that checkout exists on this machine.
    tests/test_wikilint.py patches subprocess.run itself to exercise the parsing.
    """
    def _no_subprocess(fix: bool = False):
        raise AssertionError(
            "chat.wikilint.run_lint was called in a test without a stub — it "
            "spawns the sibling lint repo, and fix=True writes to a vault."
        )

    monkeypatch.setattr(_wikilint, "run_lint", _no_subprocess)
    # The cache is module state and survives between tests: a real payload
    # cached by one test would be served to the next without run_lint firing.
    _wikilint._LINT_CACHE.clear()


@pytest.fixture(autouse=True)
def _isolate_eval_results(tmp_path, monkeypatch):
    """Send the model bake-off's output to tmp_path.

    evals/results/ holds real measured runs that took hours of Ollama time, and
    evals.score defaults to reading the NEWEST file there. A test writing a
    two-record fixture would silently become the run `score` reports on. Same
    rule as every store above: the redirect is the backstop, not the per-test
    monkeypatching."""
    results = tmp_path / "eval_results"
    monkeypatch.setattr(_run_eval, "RESULTS_DIR", results)
