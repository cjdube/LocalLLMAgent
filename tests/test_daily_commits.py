"""Tests for scribejay/daily_commits.py — that main() drafts and persists a
Daily-Commits entry, and that --backfill writes each day separately. All
collaborators are monkeypatched; nothing touches the model, git, the vault, or
Gmail."""

import sys

import pytest

from chat.insights import _is_run_success
from scribejay import daily_commits as dc

DRAFT = ("## Daily Commits: August 25, 2026\n\n### What I Built\n"
         "- **Labelled-email actions:** Wren can now act on a labelled email.")


def _commit(repo="LocalLLMAgent", subject="Answer an email from the phone"):
    return {"sha": "3fe2b23", "time": "2026-08-25T10:42:53-04:00", "repo": repo,
            "subject": subject, "files": ["agent/tools/gmail_read.py"],
            "files_total": 12, "insertions": 694, "deletions": 45}


@pytest.fixture
def stubbed_run(monkeypatch):
    """Stub every collaborator for a happy-path run: commits yesterday and a draft
    with a real bullet, so both the pre-check and the all-None post-check pass."""
    seen = {"persists": [], "drafted": 0, "prompts": [], "warms": 0, "fetches": 0}
    monkeypatch.setattr(sys, "argv", ["daily_commits"])  # argparse must not see pytest's argv
    monkeypatch.setattr(dc, "collect_commits",
                        lambda *a, **k: {"commits": [_commit()], "repos": {"LocalLLMAgent": 1},
                                         "total_commits": 1, "repos_scanned": 12})
    # The one network call in the task. Stubbed here rather than left to run
    # against the tmp PROJECTS_DIR, so a test can never reach a real remote.
    monkeypatch.setattr(dc, "fetch_repos",
                        lambda logger=None: seen.update(fetches=seen["fetches"] + 1)
                        or {"repos": 12, "failed": 0})
    monkeypatch.setattr(dc, "scribejay_backend", lambda key: None)
    monkeypatch.setattr(dc, "warm_model", lambda **k: seen.update(warms=seen["warms"] + 1))

    def _draft(**k):
        seen["drafted"] += 1
        seen["prompts"].append(k["user_prompt"])
        return DRAFT
    monkeypatch.setattr(dc, "complete_text", _draft)
    monkeypatch.setattr(dc, "persist_or_email",
                        lambda content, prefix, day, subject, task_name, logger:
                        seen["persists"].append((prefix, subject, content)) or {"written": True})
    monkeypatch.setattr(dc, "notify_failure", lambda *a, **k: None)
    return seen


def test_happy_path_persists_daily_commits(stubbed_run):
    assert dc.main() == 0
    assert len(stubbed_run["persists"]) == 1
    prefix, subject, content = stubbed_run["persists"][0]
    assert prefix == "Daily-Commits"
    assert "Daily Commits" in content


def test_the_totals_line_is_appended_in_python(stubbed_run):
    # The model is never asked for arithmetic (CLAUDE.md), so the totals must be
    # in the written page even though the draft it returned had none.
    assert "+694" not in DRAFT
    assert dc.main() == 0
    _, _, content = stubbed_run["persists"][0]
    assert "*LocalLLMAgent — 1 commit, +694/-45*" in content


def test_the_prompt_carries_subjects_paths_and_totals(stubbed_run):
    dc.main()
    prompt = stubbed_run["prompts"][0]
    assert "Answer an email from the phone" in prompt
    assert "agent/tools/gmail_read.py" in prompt   # the paths are the shape of the work
    assert "12 files, +694/-45" in prompt          # the pre-trim total, not len(files)


def test_no_commits_skips_without_calling_model(stubbed_run, monkeypatch):
    # A day with no commits is an ordinary day — skip before warming the model.
    monkeypatch.setattr(dc, "collect_commits",
                        lambda *a, **k: {"commits": [], "repos": {}, "total_commits": 0,
                                         "repos_scanned": 12})
    assert dc.main() == 0
    assert stubbed_run["persists"] == []
    assert stubbed_run["drafted"] == 0


def test_all_none_draft_skips_the_write(stubbed_run, monkeypatch):
    monkeypatch.setattr(
        dc, "complete_text",
        lambda **k: "## Daily Commits: August 25, 2026\n\n### What I Built\n"
                    "- **None:** [No qualifying items for this section]")
    assert dc.main() == 0
    assert stubbed_run["persists"] == []


def test_an_empty_draft_on_a_day_that_had_commits_is_logged_as_a_warning(stubbed_run,
                                                                        monkeypatch, capsys):
    # There WERE commits, so an all-None draft is the model failing, not a quiet
    # day. Without the WARNING the only symptom is a missing file nobody looks for
    # (CLAUDE.md: degrading is only safe if it's logged).
    monkeypatch.setattr(dc, "complete_text", lambda **k: "")
    assert dc.main() == 0
    assert stubbed_run["persists"] == []
    out = capsys.readouterr().out
    assert "[WARNING]" in out and "no bullets" in out


@pytest.mark.parametrize("quiet_day", ["no_commits", "all_none_draft"])
def test_skipped_runs_still_log_a_run_complete_boundary(stubbed_run, monkeypatch,
                                                        capsys, quiet_day):
    # The dashboard reads run status from the log: a run that logs a start and no
    # completion is reported as still "running" forever. Asserted through insights'
    # own matcher so the two can't drift apart.
    if quiet_day == "no_commits":
        monkeypatch.setattr(dc, "collect_commits",
                            lambda *a, **k: {"commits": [], "repos": {}, "total_commits": 0,
                                             "repos_scanned": 12})
    else:
        monkeypatch.setattr(dc, "complete_text", lambda **k: "")

    assert dc.main() == 0
    assert stubbed_run["persists"] == []
    assert any(_is_run_success(line) for line in capsys.readouterr().out.splitlines())


def test_collect_failure_is_a_failed_run(stubbed_run, monkeypatch):
    calls = []
    monkeypatch.setattr(dc, "collect_commits",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("git boom")))
    monkeypatch.setattr(dc, "notify_failure",
                        lambda name, detail, logger=None: calls.append(str(detail)))
    assert dc.main() == 1
    assert any("git boom" in c for c in calls)
    assert stubbed_run["persists"] == []


def test_template_filling_runs_with_thinking_off(stubbed_run, monkeypatch):
    # CLAUDE.md: a call that fills a fixed template passes think=False, because
    # thinking tokens share num_predict and an over-reasoning call returns EMPTY.
    seen = {}
    monkeypatch.setattr(dc, "complete_text", lambda **k: seen.update(k) or DRAFT)
    dc.main()
    assert seen["think"] is False
    assert seen["logger"] is not None


# --------------------------------------------------------------------------- #
# --backfill / --date
# --------------------------------------------------------------------------- #

def test_backfill_writes_one_file_per_day(stubbed_run, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["daily_commits", "--backfill", "3"])
    windows = []

    def _collect(start, end, logger=None):
        windows.append((start.date(), end.date()))
        return {"commits": [_commit()], "repos": {"LocalLLMAgent": 1},
                "total_commits": 1, "repos_scanned": 12}
    monkeypatch.setattr(dc, "collect_commits", _collect)

    assert dc.main() == 0
    assert len(stubbed_run["persists"]) == 3
    # One whole day per run, each window inside a single date, oldest first.
    assert all(s == e for s, e in windows)
    assert [w[0] for w in windows] == sorted(w[0] for w in windows)
    assert len({w[0] for w in windows}) == 3


def test_backfill_is_one_run_in_the_dashboard_history(stubbed_run, monkeypatch, capsys):
    # 14 days must read as ONE run, not 14 half-runs: chat/insights.py builds the
    # run history from the boundary lines, and a second "Starting" without an
    # ending hangs the previous one as still running.
    monkeypatch.setattr(sys, "argv", ["daily_commits", "--backfill", "3"])
    dc.main()
    lines = capsys.readouterr().out.splitlines()
    assert sum(1 for line in lines if "Starting daily commits run" in line) == 1
    assert sum(1 for line in lines if _is_run_success(line)) == 1


def test_a_run_fetches_before_it_reads(stubbed_run, monkeypatch):
    # Both halves of the guarantee: the fetch happens at all, and it happens
    # BEFORE the first collect_commits. Fetching after the read would update the
    # disk for tomorrow and still write today's page from stale objects.
    order = []
    monkeypatch.setattr(dc, "fetch_repos",
                        lambda logger=None: order.append("fetch") or {"repos": 1, "failed": 0})
    monkeypatch.setattr(dc, "collect_commits",
                        lambda *a, **k: order.append("collect") or
                        {"commits": [_commit()], "repos": {"LocalLLMAgent": 1},
                         "total_commits": 1, "repos_scanned": 12})
    assert dc.main() == 0
    assert order == ["fetch", "collect"]


def test_backfill_fetches_once_not_per_day(stubbed_run, monkeypatch):
    # A fortnight backfill needs the objects on disk one time. Fetching per day
    # would be fourteen round trips to every remote for identical results.
    monkeypatch.setattr(sys, "argv", ["daily_commits", "--backfill", "5"])
    assert dc.main() == 0
    assert stubbed_run["drafted"] == 5
    assert stubbed_run["fetches"] == 1


def test_backfill_warms_the_model_once_not_per_day(stubbed_run, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["daily_commits", "--backfill", "5"])
    dc.main()
    assert stubbed_run["drafted"] == 5
    assert stubbed_run["warms"] == 1


def test_a_backfill_of_quiet_days_never_wakes_the_model(stubbed_run, monkeypatch):
    # Loading a 26B model to write nothing is the cost this guards against.
    monkeypatch.setattr(sys, "argv", ["daily_commits", "--backfill", "5"])
    monkeypatch.setattr(dc, "collect_commits",
                        lambda *a, **k: {"commits": [], "repos": {}, "total_commits": 0,
                                         "repos_scanned": 12})
    assert dc.main() == 0
    assert stubbed_run["warms"] == 0
    assert stubbed_run["persists"] == []


def test_a_quiet_day_inside_a_backfill_does_not_stop_the_rest(stubbed_run, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["daily_commits", "--backfill", "3"])
    calls = {"n": 0}

    def _collect(start, end, logger=None):
        calls["n"] += 1
        if calls["n"] == 2:
            return {"commits": [], "repos": {}, "total_commits": 0, "repos_scanned": 12}
        return {"commits": [_commit()], "repos": {"LocalLLMAgent": 1},
                "total_commits": 1, "repos_scanned": 12}
    monkeypatch.setattr(dc, "collect_commits", _collect)

    assert dc.main() == 0
    assert len(stubbed_run["persists"]) == 2


def test_date_writes_that_day_and_only_that_day(stubbed_run, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["daily_commits", "--date", "2026-08-25"])
    days = []
    monkeypatch.setattr(dc, "persist_or_email",
                        lambda content, prefix, day, subject, task_name, logger:
                        days.append(day) or {"written": True})
    assert dc.main() == 0
    assert [str(d) for d in days] == ["2026-08-25"]
