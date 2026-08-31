"""Tests for tasks.build_queue — the store between the ClickUp tag and the
Claude Code run.

conftest redirects _STORE_PATH to tmp_path, so nothing here can leave a job in
the production store. That redirect is the important one in this file: a job
sitting in config/build_jobs.json is a paid Claude Code run that the next real
poll of tasks/build_worker.py would start.

The properties worth breaking a build over:

1. A job carries the whole plan, so it is complete without ClickUp.
2. mark_running is a claim, not a status write — a second worker is told no.
3. The pending cap refuses rather than queueing a sixth paid run.
4. A killed worker's job stops reading as live.
"""

from datetime import datetime, timedelta

import pytest

from tasks import build_queue


def _enqueue(title="Add up-arrow recall", plan="## Context\nDo the thing.\n"):
    return build_queue.enqueue("86bbnfav7", title, plan, "a-plan.md")


# --------------------------------------------------------------------------- #
# What a job carries
# --------------------------------------------------------------------------- #

def test_a_job_carries_the_whole_plan_not_a_link_to_it():
    """The plan is copied in so the job is a complete description of what will
    be built. A URL would let the attachment change, or vanish, between the tag
    and the run — and the run is what spends the money."""
    job = _enqueue(plan="line one\nline two\n")
    assert job["plan_text"] == "line one\nline two\n"
    assert job["status"] == "pending"
    assert job["task_id"] == "86bbnfav7"


def test_an_empty_plan_is_refused():
    assert "error" in build_queue.enqueue("86bb", "Some task", "   \n ", "a.md")


def test_next_pending_takes_the_oldest_first():
    first = _enqueue(title="first")
    second = _enqueue(title="second")
    assert second["id"] != first["id"]
    assert build_queue.next_pending()["id"] == first["id"]


def test_nothing_queued_means_no_next_job():
    assert build_queue.next_pending() is None


# --------------------------------------------------------------------------- #
# mark_running is the claim
# --------------------------------------------------------------------------- #

def test_marking_a_job_running_claims_it():
    job = _enqueue()
    assert build_queue.mark_running(job["id"], "wren-build/x", "/tmp/x") is True
    assert build_queue.next_pending() is None


def test_a_second_worker_is_refused_the_same_job():
    """Two overlapping runs must not both build the same plan. The claim, not
    the poll, is what excludes the loser — next_pending is a lock-free read and
    both workers can legitimately see the same job."""
    job = _enqueue()
    assert build_queue.mark_running(job["id"], "b", "/tmp/a") is True
    assert build_queue.mark_running(job["id"], "b", "/tmp/b") is False


def test_claiming_a_job_records_the_branch_and_worktree():
    job = _enqueue()
    build_queue.mark_running(job["id"], "wren-build/thing-ab12", "/tmp/thing")
    stored = build_queue.list_jobs()[0]
    assert stored["branch"] == "wren-build/thing-ab12"
    assert stored["worktree"] == "/tmp/thing"


def test_an_unknown_job_cannot_be_claimed():
    assert build_queue.mark_running("nosuchid", "b", "/tmp/x") is False


# --------------------------------------------------------------------------- #
# The cap
# --------------------------------------------------------------------------- #

def test_the_pending_cap_refuses_rather_than_queueing():
    """Not a soft cap. Each row is a paid Claude Code run, and a queue this deep
    means the worker is not draining — building more on top makes that worse."""
    for i in range(build_queue._MAX_PENDING_JOBS):
        assert "error" not in _enqueue(title=f"task {i}")
    refused = _enqueue(title="one too many")
    assert "error" in refused
    assert "full" in refused["error"]


def test_a_drained_queue_accepts_again():
    jobs = [_enqueue(title=f"task {i}") for i in range(build_queue._MAX_PENDING_JOBS)]
    build_queue.mark_done(jobs[0]["id"], "report")
    assert "error" not in _enqueue(title="room now")


# --------------------------------------------------------------------------- #
# Terminal states and pruning
# --------------------------------------------------------------------------- #

def test_a_finished_job_keeps_its_report():
    job = _enqueue()
    build_queue.mark_done(job["id"], "wren-build: branch x, tests pass")
    stored = build_queue.list_jobs()[0]
    assert stored["status"] == "done"
    assert "tests pass" in stored["report"]


def test_a_failed_job_says_why():
    job = _enqueue()
    build_queue.mark_failed(job["id"], "claude exited 1")
    assert build_queue.list_jobs()[0]["report"] == "failed: claude exited 1"


def test_listing_leaves_the_plan_text_out():
    """A plan is thousands of characters and is never the answer to "what ran?"."""
    _enqueue(plan="x" * 5000)
    assert "plan_text" not in build_queue.list_jobs()[0]


def test_terminal_jobs_are_pruned_but_live_ones_never_are():
    """The store is re-read on every worker poll, forever. A live job must
    survive that pruning no matter how many finished ones pile up behind it."""
    live = _enqueue(title="still waiting")
    for i in range(build_queue._MAX_STORED_JOBS + 10):
        done = build_queue.enqueue("86bb", f"old {i}", "plan", "a.md")
        build_queue.mark_done(done["id"], "old report")
    ids = [j["id"] for j in build_queue.list_jobs()]
    assert live["id"] in ids
    assert len(ids) <= build_queue._MAX_STORED_JOBS + 1


# --------------------------------------------------------------------------- #
# A killed worker
# --------------------------------------------------------------------------- #

def test_a_recently_claimed_job_is_not_stale():
    job = _enqueue()
    build_queue.mark_running(job["id"], "b", "/tmp/x")
    assert build_queue.stale_running() == []


def test_a_job_running_past_the_window_is_stale():
    """'running' is what the ClickUp comment told the user to expect an answer
    from, so a job whose worker was killed must stop reading as live."""
    job = _enqueue()
    build_queue.mark_running(job["id"], "b", "/tmp/x")
    later = datetime.now() + timedelta(seconds=build_queue.STALE_RUNNING_S + 60)
    assert [j["id"] for j in build_queue.stale_running(now=later)] == [job["id"]]


def test_an_unparseable_timestamp_counts_as_stale():
    """Keeping it would leave the job live forever, which is the failure this
    whole check exists to prevent."""
    job = _enqueue()
    build_queue.mark_running(job["id"], "b", "/tmp/x")
    data = build_queue._load()
    data["jobs"][0]["updated"] = "not a date"
    build_queue.atomic_write_json(build_queue._STORE_PATH, data)
    assert len(build_queue.stale_running()) == 1


def test_a_pending_job_is_never_stale():
    _enqueue()
    later = datetime.now() + timedelta(days=30)
    assert build_queue.stale_running(now=later) == []
