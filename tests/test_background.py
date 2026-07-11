"""Tests for the background-job store, state machine, and approval tokens.
_STORE_PATH is redirected to tmp and FLASK_SECRET_KEY is stubbed, so nothing
touches the real config/bg_jobs.json or the real signing key."""

import pytest

from agent.tools import background


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(background, "_STORE_PATH", tmp_path / "bg_jobs.json")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")


def _await(job_id):
    background.save_awaiting(job_id, [{"role": "user", "content": "x"}],
                            {"function": {"name": "send_email", "arguments": {}}})


def test_enqueue_persists_pending():
    out = background.run_in_background("do a thing")
    assert out["status"] == "pending"
    assert background.list_background_jobs()["count"] == 1


def test_enqueue_rejects_empty():
    assert "error" in background.run_in_background("   ")


def test_next_actionable_skips_awaiting_and_is_oldest_first():
    a = background.run_in_background("a")["id"]
    b = background.run_in_background("b")["id"]
    _await(a)  # a no longer actionable
    nxt = background.next_actionable()
    assert nxt["id"] == b


def test_resolve_job_flips_status_and_is_single_use():
    jid = background.run_in_background("x")["id"]
    _await(jid)
    assert background.resolve_job(jid, True) is True          # awaiting -> approved
    assert background.get_job_result(jid)["status"] == "approved"
    assert background.resolve_job(jid, True) is False          # replay no-ops


def test_resolve_unknown_job():
    assert background.resolve_job("nope", True) is False


def test_mark_done_clears_transient_state():
    jid = background.run_in_background("x")["id"]
    _await(jid)
    background.mark_done(jid, "all done")
    r = background.get_job_result(jid)
    assert r["status"] == "done" and r["result"] == "all done"


def test_mark_failed():
    jid = background.run_in_background("x")["id"]
    background.mark_failed(jid, "boom")
    assert background.get_job_result(jid)["status"] == "failed"


def test_bump_attempts_counts_up():
    jid = background.run_in_background("x")["id"]
    assert background.bump_attempts(jid) == 1
    assert background.bump_attempts(jid) == 2
    assert background.bump_attempts("nope") == 0


def test_mark_resumed_parks_conversation_back_in_pending():
    jid = background.run_in_background("x")["id"]
    _await(jid)
    background.resolve_job(jid, True)
    background.mark_resumed(jid, [{"role": "tool", "content": "sent"}])
    job = background.next_actionable()
    assert job["id"] == jid and job["status"] == "pending"
    assert job["messages"] == [{"role": "tool", "content": "sent"}]
    assert job["pending_call"] is None


def test_cli_approve_and_deny_resolve_a_stuck_job(capsys):
    jid = background.run_in_background("x")["id"]
    _await(jid)
    assert background.main(["--approve", jid]) == 0
    assert background.get_job_result(jid)["status"] == "approved"
    # No longer awaiting: a second resolve (or a replayed tap) does nothing.
    assert background.main(["--approve", jid]) == 1

    jid2 = background.run_in_background("y")["id"]
    _await(jid2)
    assert background.main(["--deny", jid2]) == 0
    assert background.get_job_result(jid2)["status"] == "denied"


def test_token_roundtrip_and_rejects_garbage():
    tok = background.make_approval_token("j1", "approve")
    assert background.read_approval_token(tok) == {"job": "j1", "decision": "approve"}
    assert background.read_approval_token("garbage") is None


def test_approval_actions_requires_public_url(monkeypatch):
    monkeypatch.delenv("WREN_PUBLIC_URL", raising=False)
    assert background.approval_actions("j1") is None

    monkeypatch.setenv("WREN_PUBLIC_URL", "https://host")
    acts = background.approval_actions("j1")
    assert [a["label"] for a in acts] == ["Approve", "Deny"]
    assert acts[0]["url"].startswith("https://host/api/bg/resolve?token=")
    # The two buttons carry distinct approve/deny tokens.
    assert background.read_approval_token(acts[0]["url"].split("token=")[1])["decision"] == "approve"
    assert background.read_approval_token(acts[1]["url"].split("token=")[1])["decision"] == "deny"
