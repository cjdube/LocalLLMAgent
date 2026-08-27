"""Tests for scribejay/git_activity.py.

The parsing tests run against captured real `git log --numstat` output (the exact
bytes git produced for the commits of 2026-08-25), because a hand-written fixture
of a format you also hand-wrote the parser for can only prove the two agree.

The collection tests build real git repos under tmp_path and shell out to real
git: the author filter and the day window are the parts most likely to be subtly
wrong, and neither is exercised by a string fixture. No network, and PROJECTS_DIR
is already pinned to tmp by tests/conftest.py.
"""

import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from scribejay import git_activity as ga

TZ = timezone(timedelta(hours=-4))

# Captured verbatim from `git log --numstat` in this repo. Two commits, one of
# them touching a binary-ish path so the "-" counts are covered.
REAL_LOG = (
    "\x0129355d9\x1f2026-08-25T21:09:34-04:00\x1fSay that re-applying Wren/Do does nothing\n"
    "10\t0\tdocs/mail-watch.md\n"
    "\x013fe2b23\x1f2026-08-25T10:42:53-04:00\x1fAnswer an email from the phone, in the thread it came from\n"
    "694\t45\tagent/tools/gmail_read.py\n"
    "-\t-\tchat/static/scribejay.svg\n"
)


def test_parse_log_reads_subject_paths_and_counts():
    rows = ga._parse_log(REAL_LOG, "LocalLLMAgent")
    assert [r["sha"] for r in rows] == ["29355d9", "3fe2b23"]
    first, second = rows
    assert first["subject"] == "Say that re-applying Wren/Do does nothing"
    assert first["files"] == ["docs/mail-watch.md"]
    assert (first["insertions"], first["deletions"]) == (10, 0)
    # A binary file counts as a file touched but contributes no lines.
    assert second["files_total"] == 2
    assert (second["insertions"], second["deletions"]) == (694, 45)
    assert all(r["repo"] == "LocalLLMAgent" for r in rows)


def test_parse_log_keeps_the_timestamp_whole():
    # CLAUDE.md's timezone rule: never slice an ISO stamp. The offset is what
    # makes a 21:09 commit sort and read as an evening one.
    assert ga._parse_log(REAL_LOG, "r")[0]["time"] == "2026-08-25T21:09:34-04:00"


def test_parse_log_survives_a_subject_containing_a_tab():
    # numstat lines are tab-separated and subjects are not sanitised, so a subject
    # with a tab in it must not be mistaken for a file row.
    log = "\x01abc123\x1f2026-08-25T09:00:00-04:00\x1ffix\tthe\tthing\n5\t1\ta.py\n"
    rows = ga._parse_log(log, "r")
    assert rows[0]["subject"] == "fix\tthe\tthing"
    assert rows[0]["files"] == ["a.py"]


def test_parse_log_of_an_empty_day_is_no_rows():
    assert ga._parse_log("", "r") == []


# --------------------------------------------------------------------------- #
# Compaction — both caps, and that each one says so
# --------------------------------------------------------------------------- #

class _Recorder:
    def __init__(self):
        self.warnings = []

    def warning(self, msg):
        self.warnings.append(msg)


def _commit(n=1, files=1, insertions=1):
    return {"sha": f"s{n}", "time": f"2026-08-25T{n:02d}:00:00-04:00", "repo": "r",
            "subject": f"commit {n}", "files": [f"f{i}.py" for i in range(files)],
            "files_total": files, "insertions": insertions, "deletions": 0}


def test_compact_trims_the_file_list_before_dropping_commits():
    rows = ga.compact_commits([_commit(1, files=40)])
    assert len(rows) == 1                                   # commit survives
    assert len(rows[0]["files"]) == ga.MAX_FILES_PER_COMMIT  # its file list did not
    assert rows[0]["files_total"] == 40                     # the real total is still told


def test_compact_caps_the_commit_count():
    rows = ga.compact_commits([_commit(n) for n in range(ga.MAX_COMMITS + 5)])
    assert len(rows) == ga.MAX_COMMITS


@pytest.mark.parametrize("commits,expect", [
    ([_commit(n) for n in range(ga.MAX_COMMITS + 5)], "capped commits"),
    ([_commit(1, files=40)], "trimmed the file list"),
])
def test_every_cap_that_drops_something_logs_it(commits, expect):
    # CLAUDE.md: degrading on volume is only safe if it is logged. A silently
    # shortened prompt produces a thinner page that nothing alerts on.
    logger = _Recorder()
    ga.compact_commits(commits, logger=logger)
    assert any(expect in w for w in logger.warnings), logger.warnings


def test_compaction_is_silent_when_nothing_is_dropped():
    logger = _Recorder()
    ga.compact_commits([_commit(1, files=2)], logger=logger)
    assert logger.warnings == []


def test_compact_does_not_mutate_the_collected_rows():
    original = _commit(1, files=40)
    ga.compact_commits([original])
    assert len(original["files"]) == 40


# --------------------------------------------------------------------------- #
# Rendering — the char budget, not just the row cap
# --------------------------------------------------------------------------- #

def test_render_puts_subject_totals_and_files_in_the_block():
    block = ga.render_commits([_commit(1, files=2, insertions=7)])
    assert "[r] commit 1 (2 files, +7/-0)" in block
    assert "files: f0.py, f1.py" in block


def test_render_drops_file_lists_when_over_the_char_budget():
    # A count cap alone never bounds size: these rows are under MAX_COMMITS and
    # under MAX_FILES_PER_COMMIT, and still blow the window on path length.
    rows = [dict(_commit(n), files=[f"a/very/deeply/nested/path/number/{i}/module.py"
                                    for i in range(ga.MAX_FILES_PER_COMMIT)])
            for n in range(ga.MAX_COMMITS)]
    logger = _Recorder()
    block = ga.render_commits(rows, logger=logger)
    assert len(block) <= ga.MAX_PROMPT_CHARS
    assert "files:" not in block          # the file lists went
    assert "[r] commit 0" in block        # every commit stayed
    assert any("dropped every file list" in w for w in logger.warnings), logger.warnings


# --------------------------------------------------------------------------- #
# Collection — against real repos and real git
# --------------------------------------------------------------------------- #

def _run(*args, cwd, env=None):
    subprocess.run(args, cwd=str(cwd), check=True,
                   capture_output=True, text=True, env=env)


def _repo(root, name):
    path = root / name
    path.mkdir(parents=True)
    _run("git", "init", "-q", "-b", "main", cwd=path)
    _run("git", "config", "user.name", "Fixture", cwd=path)
    _run("git", "config", "user.email", "me@example.com", cwd=path)
    return path


def _commit_at(path, filename, body, when, email="me@example.com", name="Fixture"):
    (path / filename).write_text(body)
    _run("git", "add", filename, cwd=path)
    _run("git",
         "-c", f"user.name={name}", "-c", f"user.email={email}",
         "commit", "-q", "-m", f"add {filename}",
         cwd=path,
         env={"PATH": "/usr/bin:/bin:/usr/local/bin",
              "HOME": str(path),
              "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when,
              "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
              "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email})


@pytest.fixture
def projects(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv("PROJECTS_DIR", str(root))
    monkeypatch.setenv("SCRIBEJAY_GIT_AUTHOR", "me@example.com")
    return root


YESTERDAY = datetime(2026, 8, 25, 0, 0, 0, tzinfo=TZ)
YESTERDAY_END = datetime(2026, 8, 25, 23, 59, 59, tzinfo=TZ)


def test_collect_reads_commits_from_every_checkout(projects):
    alpha = _repo(projects, "alpha")
    beta = _repo(projects, "beta")
    _commit_at(alpha, "a.py", "x\ny\n", "2026-08-25T10:00:00-04:00")
    _commit_at(beta, "b.py", "z\n", "2026-08-25T14:00:00-04:00")

    out = ga.collect_commits(YESTERDAY, YESTERDAY_END)
    assert out["total_commits"] == 2
    assert out["repos"] == {"alpha": 1, "beta": 1}
    assert out["repos_scanned"] == 2
    # Newest first, so the draft reads the day backwards from its last change.
    assert [c["repo"] for c in out["commits"]] == ["beta", "alpha"]
    assert out["commits"][1]["files"] == ["a.py"]
    assert out["commits"][1]["insertions"] == 2


def test_collect_excludes_commits_outside_the_day(projects):
    repo = _repo(projects, "alpha")
    _commit_at(repo, "before.py", "x\n", "2026-08-24T23:59:00-04:00")
    _commit_at(repo, "during.py", "x\n", "2026-08-25T12:00:00-04:00")
    _commit_at(repo, "after.py", "x\n", "2026-08-26T00:30:00-04:00")

    out = ga.collect_commits(YESTERDAY, YESTERDAY_END)
    assert [c["subject"] for c in out["commits"]] == ["add during.py"]


def test_collect_excludes_a_late_evening_commit_from_the_wrong_day(projects):
    # The timezone trap CLAUDE.md names: 2026-08-25T22:30-04:00 is 2026-08-26 in
    # UTC. A window built from UTC stamps would file this under the wrong day, and
    # a fixture timed before 20:00 would never notice.
    repo = _repo(projects, "alpha")
    _commit_at(repo, "evening.py", "x\n", "2026-08-25T22:30:00-04:00")

    assert ga.collect_commits(YESTERDAY, YESTERDAY_END)["total_commits"] == 1
    day_after = datetime(2026, 8, 26, 0, 0, 0, tzinfo=TZ)
    assert ga.collect_commits(day_after, day_after.replace(hour=23))["total_commits"] == 0


def test_collect_excludes_another_authors_commits(projects):
    repo = _repo(projects, "alpha")
    _commit_at(repo, "mine.py", "x\n", "2026-08-25T10:00:00-04:00")
    _commit_at(repo, "theirs.py", "x\n", "2026-08-25T11:00:00-04:00",
               email="someone@else.example", name="Someone Else")

    out = ga.collect_commits(YESTERDAY, YESTERDAY_END)
    assert [c["subject"] for c in out["commits"]] == ["add mine.py"]


def test_collect_excludes_merge_commits(projects):
    repo = _repo(projects, "alpha")
    _commit_at(repo, "base.py", "x\n", "2026-08-25T09:00:00-04:00")
    _run("git", "checkout", "-q", "-b", "side", cwd=repo)
    _commit_at(repo, "side.py", "x\n", "2026-08-25T09:30:00-04:00")
    _run("git", "checkout", "-q", "main", cwd=repo)
    _commit_at(repo, "main.py", "x\n", "2026-08-25T09:45:00-04:00")
    _run("git", "merge", "-q", "--no-ff", "-m", "merge side", "side", cwd=repo,
         # Dated INTO the window on purpose. Left undated the merge lands at "now"
         # and falls outside the day, which makes this test pass whether or not
         # --no-merges is there at all.
         env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(repo),
              "GIT_AUTHOR_DATE": "2026-08-25T10:00:00-04:00",
              "GIT_COMMITTER_DATE": "2026-08-25T10:00:00-04:00",
              "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "me@example.com",
              "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "me@example.com"})

    subjects = [c["subject"] for c in ga.collect_commits(YESTERDAY, YESTERDAY_END)["commits"]]
    assert "merge side" not in subjects
    # HEAD only, but the merge brought the side branch's commit into it.
    assert sorted(subjects) == ["add base.py", "add main.py", "add side.py"]


def test_collect_ignores_a_directory_that_is_not_a_repo(projects):
    (projects / "notes").mkdir()
    repo = _repo(projects, "alpha")
    _commit_at(repo, "a.py", "x\n", "2026-08-25T10:00:00-04:00")

    out = ga.collect_commits(YESTERDAY, YESTERDAY_END)
    assert out["repos_scanned"] == 1
    assert out["total_commits"] == 1


def test_collect_on_an_empty_projects_dir_is_an_ordinary_empty_day(projects):
    out = ga.collect_commits(YESTERDAY, YESTERDAY_END)
    assert out == {"commits": [], "repos": {}, "total_commits": 0, "repos_scanned": 0}


def test_a_broken_checkout_is_skipped_and_logged_not_raised(projects, monkeypatch):
    good = _repo(projects, "alpha")
    _commit_at(good, "a.py", "x\n", "2026-08-25T10:00:00-04:00")
    _repo(projects, "broken")

    real_git = ga._git
    monkeypatch.setattr(ga, "_git",
                        lambda path, *args: None if path.name == "broken" else real_git(path, *args))
    logger = _Recorder()
    out = ga.collect_commits(YESTERDAY, YESTERDAY_END, logger=logger)
    assert out["total_commits"] == 1
    assert any("broken" in w for w in logger.warnings), logger.warnings


def test_no_resolvable_author_warns_that_everyone_counts(projects, monkeypatch):
    repo = _repo(projects, "alpha")
    _commit_at(repo, "theirs.py", "x\n", "2026-08-25T10:00:00-04:00",
               email="someone@else.example", name="Someone Else")
    monkeypatch.delenv("SCRIBEJAY_GIT_AUTHOR", raising=False)
    monkeypatch.setattr(ga, "author", lambda: None)

    logger = _Recorder()
    out = ga.collect_commits(YESTERDAY, YESTERDAY_END, logger=logger)
    # Both halves: it really does take everyone's commits, AND it says so.
    assert out["total_commits"] == 1
    assert any("EVERY author" in w for w in logger.warnings), logger.warnings


def test_author_prefers_the_configured_identity(monkeypatch):
    monkeypatch.setenv("SCRIBEJAY_GIT_AUTHOR", "work@example.com")
    assert ga.author() == "work@example.com"


# --------------------------------------------------------------------------- #
# Fetching — the commit made on another machine
#
# "Another machine" is a second clone of the same bare remote. Nothing here
# touches the network: every remote is a path under tmp_path.
# --------------------------------------------------------------------------- #

def _bare(path):
    path.mkdir(parents=True)
    _run("git", "init", "-q", "--bare", "-b", "main", cwd=path)
    return path


@pytest.fixture
def pushed(projects, tmp_path):
    """One repo under PROJECTS_DIR and one "laptop" clone, sharing a bare remote.

    Each has committed once on the same day, and both have pushed. PROJECTS_DIR's
    copy has never fetched, so the laptop's commit exists on the remote and
    nowhere on this disk — exactly the state a 4:55 AM run finds."""
    bare = _bare(tmp_path / "origin.git")
    alpha = _repo(projects, "alpha")
    _commit_at(alpha, "here.py", "x\n", "2026-08-25T10:00:00-04:00")
    _run("git", "remote", "add", "origin", str(bare), cwd=alpha)
    _run("git", "push", "-q", "origin", "main", cwd=alpha)

    laptop = tmp_path / "laptop"
    _run("git", "clone", "-q", str(bare), str(laptop), cwd=tmp_path)
    _commit_at(laptop, "laptop.py", "y\n", "2026-08-25T18:00:00-04:00")
    _run("git", "push", "-q", "origin", "main", cwd=laptop)
    return alpha


def test_a_commit_pushed_from_another_machine_is_missed_until_fetch(pushed):
    # Both halves: the gap is real, and fetch_repos is what closes it. Asserting
    # only the second half would stay green if the scan had never needed a fetch.
    before = ga.collect_commits(YESTERDAY, YESTERDAY_END)
    assert [c["subject"] for c in before["commits"]] == ["add here.py"]

    assert ga.fetch_repos() == {"repos": 1, "failed": 0}

    after = ga.collect_commits(YESTERDAY, YESTERDAY_END)
    assert sorted(c["subject"] for c in after["commits"]) == ["add here.py", "add laptop.py"]


def test_a_commit_on_both_head_and_the_remote_is_counted_once(pushed):
    # here.py is reachable from HEAD and from origin/main. Scanning both refs must
    # not double the day's totals.
    ga.fetch_repos()
    out = ga.collect_commits(YESTERDAY, YESTERDAY_END)
    assert out["total_commits"] == 2
    assert out["repos"] == {"alpha": 2}


def test_a_repo_with_no_remote_is_a_no_op_not_a_failure(projects):
    # Several of the user's checkouts have never been pushed anywhere.
    repo = _repo(projects, "alpha")
    _commit_at(repo, "a.py", "x\n", "2026-08-25T10:00:00-04:00")

    logger = _Recorder()
    assert ga.fetch_repos(logger=logger) == {"repos": 1, "failed": 0}
    assert logger.warnings == []


def test_an_unreachable_remote_fails_without_raising(projects, tmp_path):
    repo = _repo(projects, "alpha")
    _commit_at(repo, "a.py", "x\n", "2026-08-25T10:00:00-04:00")
    _run("git", "remote", "add", "origin", str(tmp_path / "nowhere.git"), cwd=repo)

    logger = _Recorder()
    assert ga.fetch_repos(logger=logger) == {"repos": 1, "failed": 1}
    assert any("alpha" in w for w in logger.warnings), logger.warnings


def test_a_failed_fetch_still_leaves_the_day_readable(projects, monkeypatch):
    # GitHub being down must cost the newest commits, never the whole entry.
    repo = _repo(projects, "alpha")
    _commit_at(repo, "a.py", "x\n", "2026-08-25T10:00:00-04:00")
    monkeypatch.setattr(ga, "_fetch", lambda path: False)

    logger = _Recorder()
    assert ga.fetch_repos(logger=logger)["failed"] == 1
    assert ga.collect_commits(YESTERDAY, YESTERDAY_END)["total_commits"] == 1


def test_fetch_can_never_sit_waiting_for_a_password(projects, monkeypatch):
    # A 4:55 AM run has no terminal. Without these, a remote asking for a password
    # or an unknown host key blocks until FETCH_TIMEOUT every single morning.
    repo = _repo(projects, "alpha")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["env"] = kwargs.get("env") or {}
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ga.subprocess, "run", fake_run)
    assert ga._fetch(repo) is True
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert "BatchMode=yes" in seen["env"]["GIT_SSH_COMMAND"]
    assert seen["timeout"] == ga.FETCH_TIMEOUT
