"""Tests for tasks.build_worker — the module that actually runs Claude Code.

Nothing here spawns a process. conftest's autouse _block_build_subprocess
rebinds this module's `subprocess` global to a stub that raises, and every test
below that needs a process substitutes its own recorder in its place. That guard
matters more here than anywhere else in the suite: the three things this module
shells out to are `git worktree add` against the user's real checkout, a paid
`claude -p` run that edits files for tens of minutes, and a nested pytest.

The promises worth breaking a build over, each asserted on its own:

1. The plan reaches Claude verbatim, and the prompt forbids every git write.
2. The deny list covers the git verbs that write, and leaves the ones that read.
3. A commit that happens anyway is undone AND reported — both halves.
4. Every failure path still reports; a build that dies quietly reads exactly
   like one that never started.
"""

import json
import logging
import subprocess
from pathlib import Path

import pytest

from tasks import build_queue, build_worker


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #

class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class _FakeSubprocess:
    """Records every argv and answers from a routing table. Keeps the real
    TimeoutExpired so the module's own `except` clauses still match."""

    TimeoutExpired = subprocess.TimeoutExpired
    CompletedProcess = subprocess.CompletedProcess

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        for key, value in self.answers.items():
            if key in " ".join(argv):
                if isinstance(value, Exception):
                    raise value
                return value
        return _Proc()


@pytest.fixture
def fake_run(monkeypatch):
    def _install(answers=None):
        fake = _FakeSubprocess(answers)
        monkeypatch.setattr(build_worker, "subprocess", fake)
        return fake
    return _install


@pytest.fixture
def logger():
    log = logging.getLogger("test_build_worker")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    log.propagate = True
    return log


@pytest.fixture
def reported(monkeypatch):
    """Capture both report channels. Neither may be allowed to silence the
    other, so they are recorded separately."""
    pushes, comments = [], []
    monkeypatch.setattr(build_worker, "notify",
                        lambda message, title=None, **k: pushes.append((title, message)))
    monkeypatch.setattr(build_worker.clickup, "comment_on_clickup_task",
                        lambda title, comment, **k: comments.append((title, comment)) or {})
    return type("Reported", (), {"pushes": pushes, "comments": comments})()


def _job(**over):
    job = {"id": "ab12cd34", "task_id": "86bbnfav7", "title": "Add up-arrow recall",
           "description": "", "plan_name": "a-plan.md",
           "plan_text": "## Context\nDo exactly this.\n"}
    job.update(over)
    return job


# --------------------------------------------------------------------------- #
# The prompt Python writes
# --------------------------------------------------------------------------- #

def test_the_plan_reaches_claude_verbatim():
    """Nothing summarises or re-words the plan. The user wrote and reviewed that
    text; every rewrite is a chance to drop a step or invent one."""
    plan = "## Step 1\nEdit agent/toolset.py.\n\n## Step 2\nRun pytest.\n"
    prompt = build_worker.build_prompt(_job(plan_text=plan), Path("/tmp/wt"), "wren-build/x")
    assert plan in prompt


def test_the_prompt_forbids_every_git_write():
    prompt = build_worker.build_prompt(_job(), Path("/tmp/wt"), "wren-build/x")
    for forbidden in ("commit", "git add", "push", "gh", "branch"):
        assert forbidden in prompt, f"the prompt never mentions {forbidden}"
    assert "unstaged" in prompt


def test_the_prompt_names_the_worktree_and_branch():
    prompt = build_worker.build_prompt(_job(), Path("/tmp/wt/thing"), "wren-build/thing-ab12")
    assert "/tmp/wt/thing" in prompt
    assert "wren-build/thing-ab12" in prompt


def test_the_prompt_points_at_agents_md_and_the_test_command():
    """AGENTS.md is where every constraint in this repo lives, and the pytest
    line is the exact command that file documents."""
    prompt = build_worker.build_prompt(_job(), Path("/tmp/wt"), "b")
    assert "AGENTS.md" in prompt
    assert ".venv/bin/python -m pytest" in prompt


def test_the_prompt_carries_the_task_title_and_description():
    prompt = build_worker.build_prompt(
        _job(description="Up-arrow should recall the last message"), Path("/tmp/wt"), "b")
    assert "Add up-arrow recall" in prompt
    assert "Up-arrow should recall the last message" in prompt


def test_an_empty_description_leaves_no_dangling_header():
    prompt = build_worker.build_prompt(_job(description=""), Path("/tmp/wt"), "b")
    assert "What the task says" not in prompt


def test_the_prompt_never_carries_the_clickup_id():
    """Same rule as the watcher's own templates: an opaque id has no business in
    a prompt (docs/opaque-identifiers.md), and here it would only be noise."""
    prompt = build_worker.build_prompt(_job(task_id="86bbzzzzz"), Path("/tmp/wt"), "b")
    assert "86bbzzzzz" not in prompt


# --------------------------------------------------------------------------- #
# The deny list and the command line
# --------------------------------------------------------------------------- #

def test_the_deny_list_covers_every_git_verb_that_writes():
    deny = json.loads(build_worker.settings_json())["permissions"]["deny"]
    joined = " ".join(deny)
    for verb in ("commit", "push", "add", "checkout", "switch", "branch", "reset",
                 "rebase", "merge", "stash", "worktree", "tag", "cherry-pick",
                 "restore", "clean", "remote"):
        assert f"git {verb}:" in joined, f"git {verb} is not denied"
    assert "Bash(gh:*)" in deny


def test_the_deny_list_leaves_read_only_git_alone():
    """A plan may legitimately need to look at history. Denying `git log` would
    make a whole class of plan quietly impossible to implement."""
    joined = " ".join(json.loads(build_worker.settings_json())["permissions"]["deny"])
    for verb in ("status", "diff", "log", "show"):
        assert f"git {verb}:" not in joined


def test_the_command_line_loads_no_mcp_servers():
    """An unattended build has no business holding the user's Gmail, Drive or
    database tools. --strict-mcp-config with no --mcp-config loads none."""
    argv = build_worker.claude_argv("prompt", Path("/tmp/s.json"))
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" not in argv


def test_the_command_line_asks_for_json_and_passes_the_settings_file():
    argv = build_worker.claude_argv("prompt", Path("/tmp/s.json"))
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--settings") + 1] == "/tmp/s.json"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


def test_the_allowed_tools_are_one_comma_separated_value():
    """--allowedTools is variadic, so separate argv items can swallow the flag
    that follows. One comma-separated value cannot."""
    argv = build_worker.claude_argv("prompt", Path("/tmp/s.json"))
    value = argv[argv.index("--allowedTools") + 1]
    assert value.startswith("Bash,")
    assert argv[argv.index("--allowedTools") + 2].startswith("--")


def test_the_model_flag_appears_only_when_it_is_configured(monkeypatch):
    monkeypatch.delenv("WREN_BUILD_MODEL", raising=False)
    assert "--model" not in build_worker.claude_argv("p", Path("/tmp/s.json"))
    monkeypatch.setenv("WREN_BUILD_MODEL", "opus")
    argv = build_worker.claude_argv("p", Path("/tmp/s.json"))
    assert argv[argv.index("--model") + 1] == "opus"


# --------------------------------------------------------------------------- #
# The git promise, proved rather than trusted
# --------------------------------------------------------------------------- #

def test_an_untouched_head_passes(fake_run):
    fake_run({"rev-parse": _Proc(stdout="abc123\n")})
    assert build_worker.verify_untouched_git(Path("/tmp/wt"), "abc123", "b") == {"ok": True}


def test_a_commit_is_undone_and_reported(fake_run):
    """Both halves. A reset with no report leaves the user believing the deny
    list held; a report with no reset leaves a commit on the branch."""
    fake = fake_run({"rev-parse": _Proc(stdout="def456\n"), "reset": _Proc()})
    result = build_worker.verify_untouched_git(Path("/tmp/wt"), "abc123", "b")
    assert result["ok"] is False
    assert "committed" in result["note"]
    assert any("reset" in " ".join(argv) and "abc123" in " ".join(argv)
               for argv, _ in fake.calls), "HEAD was never reset back"


def test_the_reset_keeps_the_work_in_the_working_tree(fake_run):
    """--mixed, never --hard. The edits are the whole deliverable; only the
    commit is being undone."""
    fake = fake_run({"rev-parse": _Proc(stdout="def456\n"), "reset": _Proc()})
    build_worker.verify_untouched_git(Path("/tmp/wt"), "abc123", "b")
    reset = next(argv for argv, _ in fake.calls if "reset" in argv)
    assert "--mixed" in reset
    assert "--hard" not in reset


def test_a_failed_reset_says_so_loudly(fake_run):
    fake_run({"rev-parse": _Proc(stdout="def456\n"),
              "reset": _Proc(stderr="fatal: nope", returncode=1)})
    note = build_worker.verify_untouched_git(Path("/tmp/wt"), "abc123", "b")["note"]
    assert "RESET FAILED" in note


def test_untracked_files_are_counted_as_changes(fake_run):
    """Nothing is staged, so `git diff` alone reports a brand new module as no
    change at all — which is exactly what a plan that adds a file produces."""
    fake_run({"diff": _Proc(stdout=" 2 files changed, 30 insertions(+)\n"),
              "ls-files": _Proc(stdout="tasks/new_thing.py\ntests/test_new_thing.py\n")})
    line = build_worker.diffstat(Path("/tmp/wt"), "abc123")
    assert "2 files changed" in line
    assert "2 new file(s)" in line


def test_the_workers_own_symlinks_are_not_counted_as_the_users_work(fake_run):
    """Measured on the first live build: `.gitignore` says `.venv/`, and a
    trailing slash matches a directory — so the symlink the worker itself put in
    the worktree is NOT ignored there, and was reported as '1 new file(s)'."""
    fake_run({"diff": _Proc(stdout=" 2 files changed, 183 insertions(+)\n"),
              "ls-files": _Proc(stdout=".venv\nconfig/.env\n")})
    line = build_worker.diffstat(Path("/tmp/wt"), "abc123")
    assert "new file(s)" not in line


def test_a_real_new_file_still_counts_beside_the_symlinks(fake_run):
    """The other half: discounting the plumbing must not discount real work."""
    fake_run({"diff": _Proc(stdout=" 1 file changed, 2 insertions(+)\n"),
              "ls-files": _Proc(stdout=".venv\ntasks/new_thing.py\n")})
    assert "1 new file(s)" in build_worker.diffstat(Path("/tmp/wt"), "abc123")


def test_the_linker_and_the_diff_count_read_the_same_list():
    """Two places must agree about what the worker created. If a third path is
    ever linked in, this is what fails rather than a wrong number on a Task."""
    assert build_worker.LINKED_PATHS == (".venv", "config/.env")


# --------------------------------------------------------------------------- #
# Trimming Claude's summary
# --------------------------------------------------------------------------- #

def test_a_short_summary_is_left_exactly_alone():
    text = "Done.\n\nI changed one file and the tests pass."
    assert build_worker.trim_summary(text) == text


def test_a_long_summary_is_cut_on_a_line_boundary_not_mid_word():
    """Measured on the first live build: the comment ended on the word 'every',
    which reads as a crashed job rather than a trimmed one."""
    body = "\n".join(f"- line {i} with a fair amount of text on it" for i in range(80))
    out = build_worker.trim_summary(body, limit=200)
    kept = out.split("\n[... trimmed")[0]
    assert kept in body                      # every kept line is verbatim
    assert kept.endswith("on it")            # and none of them is half a line
    assert len(kept) <= 200


def test_a_trimmed_summary_says_it_was_trimmed():
    out = build_worker.trim_summary("x " * 2000)
    assert "trimmed" in out


def test_one_enormous_line_is_still_cut_rather_than_sent_whole():
    """The fallback. A single unbroken paragraph has no boundary to cut on, and
    sending it whole would defeat the cap it is meant to respect."""
    out = build_worker.trim_summary("word " * 500, limit=100)
    assert len(out.split("\n[... trimmed")[0]) <= 100


def test_the_full_summary_reaches_the_log_because_the_trim_promises_it(
        fake_run, tmp_path, logger, caplog):
    """Both halves of one promise: the comment says to look in the build log, so
    the build log must actually hold the untrimmed text."""
    long_summary = "\n".join(f"line {i}" for i in range(400))
    fake_run({"claude": _Proc(stdout=json.dumps(
        {"result": long_summary, "total_cost_usd": 1.0, "num_turns": 3,
         "duration_ms": 1000, "is_error": False}))})
    with caplog.at_level(logging.INFO, logger="test_build_worker"):
        build_worker.run_claude(tmp_path, "a prompt", logger)
    assert any(long_summary in r.getMessage() for r in caplog.records)
    assert "trimmed" in build_worker.report_text(
        {}, "b", Path("/tmp/wt"), "c", "t", {"summary": long_summary})


# --------------------------------------------------------------------------- #
# Running claude
# --------------------------------------------------------------------------- #

@pytest.fixture
def worktree(tmp_path, monkeypatch):
    """A real directory for the settings and prompt files run_claude writes,
    with the binary check satisfied."""
    wt = tmp_path / "builds" / "thing-ab12"
    wt.mkdir(parents=True)
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setenv("WREN_CLAUDE_BIN", str(binary))
    monkeypatch.setenv("WREN_BUILD_WORKTREE_ROOT", str(tmp_path / "builds"))
    return wt


def _claude_json(**over):
    payload = {"result": "Added the recall handler.", "is_error": False,
               "total_cost_usd": 1.23, "num_turns": 42, "duration_ms": 480000}
    payload.update(over)
    return _Proc(stdout=json.dumps(payload))


def test_a_finished_run_returns_the_summary_and_cost(fake_run, worktree, logger):
    fake_run({"claude": _claude_json()})
    result = build_worker.run_claude(worktree, "prompt", logger)
    assert result["summary"] == "Added the recall handler."
    assert result["cost"] == 1.23


def test_the_settings_and_prompt_are_written_beside_the_worktree(fake_run, worktree, logger):
    """When a build goes wrong the first question is always what it was actually
    asked, so the prompt is kept on disk rather than only in the log."""
    fake_run({"claude": _claude_json()})
    build_worker.run_claude(worktree, "the exact prompt", logger)
    assert "git commit" in (worktree.parent / "thing-ab12.settings.json").read_text()
    assert (worktree.parent / "thing-ab12.prompt.txt").read_text() == "the exact prompt"


def test_a_missing_claude_binary_is_an_error_not_a_crash(monkeypatch, tmp_path, logger):
    monkeypatch.setenv("WREN_CLAUDE_BIN", str(tmp_path / "nope"))
    result = build_worker.run_claude(tmp_path, "prompt", logger)
    assert "not found" in result["error"]


def test_a_timeout_is_reported_not_raised(fake_run, worktree, logger):
    fake_run({"claude": subprocess.TimeoutExpired(cmd="claude", timeout=1800)})
    assert "did not finish" in build_worker.run_claude(worktree, "p", logger)["error"]


def test_unparseable_output_is_reported_with_what_was_said(fake_run, worktree, logger):
    fake_run({"claude": _Proc(stdout="not json", stderr="boom", returncode=1)})
    error = build_worker.run_claude(worktree, "p", logger)["error"]
    assert "no usable JSON" in error
    assert "boom" in error


def test_claudes_own_error_flag_is_believed(fake_run, worktree, logger):
    """exit 0 with is_error set is the shape a refused or aborted run takes."""
    fake_run({"claude": _claude_json(is_error=True, result="hit the turn limit")})
    assert "turn limit" in build_worker.run_claude(worktree, "p", logger)["error"]


# --------------------------------------------------------------------------- #
# The test run afterwards
# --------------------------------------------------------------------------- #

def test_a_passing_suite_reports_its_own_line(fake_run, tmp_path):
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    fake_run({"pytest": _Proc(stdout="....\n1183 passed in 41.20s\n")})
    assert "1183 passed" in build_worker.run_pytest(tmp_path)


def test_a_failing_suite_is_marked_failing(fake_run, tmp_path):
    """Red tests are reported, never hidden — the whole point of measuring this
    here rather than believing the summary."""
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    fake_run({"pytest": _Proc(stdout="3 failed, 1180 passed in 44s\n", returncode=1)})
    verdict = build_worker.run_pytest(tmp_path)
    assert verdict.startswith("FAILING")
    assert "3 failed" in verdict


def test_a_worktree_without_a_venv_says_so_rather_than_claiming_a_pass(tmp_path):
    assert "no .venv" in build_worker.run_pytest(tmp_path)


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #

def test_the_report_names_the_branch_the_tests_and_the_no_commit_promise():
    text = build_worker.report_text(
        _job(), "wren-build/thing-ab12", Path("/tmp/wt"), "4 files changed",
        "1183 passed", {"summary": "Did the thing.", "cost": 1.2, "turns": 30,
                        "duration_ms": 300000})
    assert "wren-build/thing-ab12" in text
    assert "1183 passed" in text
    assert "4 files changed" in text
    assert "Did the thing." in text
    assert "Nothing was committed or pushed" in text


def test_a_git_violation_is_shouted_in_the_report():
    text = build_worker.report_text(
        _job(), "b", Path("/tmp/wt"), "x", "y", {"summary": ""},
        git_note="Claude committed despite the deny list")
    assert "WARNING" in text
    assert "committed despite" in text


# --------------------------------------------------------------------------- #
# run_job end to end
# --------------------------------------------------------------------------- #

@pytest.fixture
def queued(monkeypatch, tmp_path):
    """A queued job whose worktree creation is stubbed out — the git side is
    covered above, and these tests are about what happens after it."""
    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setattr(build_worker, "create_worktree", lambda job, logger: {
        "worktree": wt, "branch": "wren-build/thing-ab12", "base_sha": "abc123"})
    monkeypatch.setattr(build_worker, "verify_untouched_git",
                        lambda *a, **k: {"ok": True})
    monkeypatch.setattr(build_worker, "diffstat", lambda *a: "4 files changed")
    monkeypatch.setattr(build_worker, "run_pytest", lambda *a: "1183 passed")
    return build_queue.enqueue("86bb", "Add up-arrow recall", "## Plan\ndo it\n", "a.md")


def test_a_finished_build_is_marked_done_and_reported_both_ways(
        queued, monkeypatch, reported, logger):
    monkeypatch.setattr(build_worker, "run_claude",
                        lambda *a, **k: {"summary": "Done.", "cost": 1.0,
                                         "turns": 5, "duration_ms": 60000})
    build_worker.run_job(build_queue.next_pending(), logger)
    assert build_queue.list_jobs()[0]["status"] == "done"
    assert len(reported.pushes) == 1
    assert len(reported.comments) == 1
    assert "wren-build/thing-ab12" in reported.comments[0][1]


def test_a_failed_build_still_reports(queued, monkeypatch, reported, logger):
    """A build that dies quietly is indistinguishable from one that never
    started, and the tag is already gone by then."""
    monkeypatch.setattr(build_worker, "run_claude",
                        lambda *a, **k: {"error": "claude exited 1"})
    build_worker.run_job(build_queue.next_pending(), logger)
    assert build_queue.list_jobs()[0]["status"] == "failed"
    assert "claude exited 1" in reported.pushes[0][1]
    assert "claude exited 1" in reported.comments[0][1]


def test_a_worktree_that_cannot_be_made_reports_and_never_claims_the_job(
        monkeypatch, reported, logger, tmp_path):
    build_queue.enqueue("86bb", "Add up-arrow recall", "## Plan\n", "a.md")
    monkeypatch.setattr(build_worker, "create_worktree",
                        lambda job, logger: {"error": "no git repository at /nope"})
    build_worker.run_job(build_queue.next_pending(), logger)
    stored = build_queue.list_jobs()[0]
    assert stored["status"] == "failed"
    assert stored["branch"] is None
    assert "no git repository" in reported.comments[0][1]


def test_a_job_claimed_by_another_run_is_left_alone(queued, monkeypatch, reported, logger):
    """Two workers can legitimately see the same pending job; only one may build
    it, and the loser must not report over the winner."""
    job = build_queue.next_pending()
    build_queue.mark_running(job["id"], "other", "/tmp/other")
    calls = []
    monkeypatch.setattr(build_worker, "run_claude",
                        lambda *a, **k: calls.append(1) or {"summary": "x"})
    build_worker.run_job(job, logger)
    assert calls == []
    assert reported.pushes == []


def test_a_git_violation_survives_into_the_report(queued, monkeypatch, reported, logger):
    monkeypatch.setattr(build_worker, "verify_untouched_git",
                        lambda *a, **k: {"ok": False, "note": "Claude committed anyway"})
    monkeypatch.setattr(build_worker, "run_claude",
                        lambda *a, **k: {"summary": "Done.", "cost": 1.0,
                                         "turns": 5, "duration_ms": 60000})
    build_worker.run_job(build_queue.next_pending(), logger)
    assert "Claude committed anyway" in reported.comments[0][1]


def test_a_failed_push_does_not_silence_the_clickup_comment(
        queued, monkeypatch, reported, logger):
    """Neither channel may be allowed to take the other down with it."""
    monkeypatch.setattr(build_worker, "notify",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("ntfy down")))
    monkeypatch.setattr(build_worker, "run_claude",
                        lambda *a, **k: {"summary": "Done.", "cost": 1.0,
                                         "turns": 5, "duration_ms": 60000})
    build_worker.run_job(build_queue.next_pending(), logger)
    assert len(reported.comments) == 1


# --------------------------------------------------------------------------- #
# The idle pass
# --------------------------------------------------------------------------- #

def test_a_killed_workers_job_is_failed_and_announced(monkeypatch, reported, logger):
    job = build_queue.enqueue("86bb", "Add up-arrow recall", "## Plan\n", "a.md")
    build_queue.mark_running(job["id"], "wren-build/x", "/tmp/x")
    monkeypatch.setattr(build_queue, "stale_running",
                        lambda *a, **k: [dict(build_queue.list_jobs()[0])])
    build_worker._fail_stale(logger)
    assert build_queue.list_jobs()[0]["status"] == "failed"
    assert "interrupted" in reported.comments[0][1]


def test_nothing_pending_is_not_a_failed_launchd_job(monkeypatch, logger):
    monkeypatch.setattr(build_worker, "setup_logger", lambda name: logger)
    assert build_worker.main([]) == 0


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #

def test_a_slug_is_safe_for_a_branch_and_a_path():
    assert build_worker.slug("ClickUp: wren-build / invoke Claude!") \
        == "clickup-wren-build-invoke-claude"


def test_a_slug_is_bounded():
    assert len(build_worker.slug("word " * 60)) <= 48


def test_a_title_with_nothing_usable_still_makes_a_name():
    assert build_worker.slug("!!! ???") == "build"
