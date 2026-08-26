"""Tests for scribejay/daily_chrome_learnings.py — that main() drafts and persists a
Daily-Chrome entry. All collaborators are monkeypatched; nothing touches the
model, Chrome, the vault, or Gmail."""

import pytest

from chat.insights import _is_run_success
from scribejay import daily_chrome_learnings as dc


@pytest.fixture
def stubbed_run(monkeypatch):
    """Stub every collaborator for a happy-path run: some browsing yesterday and a
    draft with a real bullet, so both the pre-check and the all-None post-check pass."""
    seen = {"persists": [], "drafted": 0}
    monkeypatch.setattr(dc, "fetch_chrome_history",
                        lambda *a, **k: {"sites": [{"domain": "github.com", "title": "gh", "visits": 3}]})
    monkeypatch.setattr(dc, "scribejay_backend", lambda key: None)
    monkeypatch.setattr(dc, "warm_model", lambda **k: True)

    def _draft(**k):
        seen["drafted"] += 1
        return "## Daily Log: July 12, 2026\n\n### Tools & Tech Encountered\n- **GitHub:** reviewed a PR"
    monkeypatch.setattr(dc, "complete_text", _draft)
    monkeypatch.setattr(dc, "persist_or_email",
                        lambda content, prefix, day, subject, task_name, logger:
                        seen["persists"].append((prefix, subject, content)) or {"written": True})
    monkeypatch.setattr(dc, "notify_failure", lambda *a, **k: None)
    return seen


def test_happy_path_persists_daily_chrome(stubbed_run):
    assert dc.main() == 0
    assert len(stubbed_run["persists"]) == 1
    prefix, subject, content = stubbed_run["persists"][0]
    assert prefix == "Daily-Chrome"
    assert "Daily Log" in content


def test_no_browsing_skips_without_calling_model(stubbed_run, monkeypatch):
    # Nothing happened yesterday — skip early, before warming the model.
    monkeypatch.setattr(dc, "fetch_chrome_history", lambda *a, **k: {"sites": []})
    assert dc.main() == 0
    assert stubbed_run["persists"] == []
    assert stubbed_run["drafted"] == 0  # model never ran


def test_all_none_draft_skips_the_write(stubbed_run, monkeypatch):
    # There was browsing, but the model found nothing relevant (all sections None).
    monkeypatch.setattr(
        dc, "complete_text",
        lambda **k: "## Daily Log: July 12, 2026\n\n### Tools & Tech Encountered\n"
                    "- **None:** [No qualifying items for this section]")
    assert dc.main() == 0
    assert stubbed_run["persists"] == []


@pytest.mark.parametrize("quiet_day", ["no_browsing", "all_none_draft"])
def test_skipped_runs_still_log_a_run_complete_boundary(stubbed_run, monkeypatch, capsys, quiet_day):
    # The dashboard reads run status from the log: a run that logs a start and no
    # completion is reported as still "running" forever. Both skip paths used to
    # return 0 without one. Asserted through insights' own matcher so the two
    # can't drift apart.
    if quiet_day == "no_browsing":
        monkeypatch.setattr(dc, "fetch_chrome_history", lambda *a, **k: {"sites": []})
    else:
        monkeypatch.setattr(
            dc, "complete_text",
            lambda **k: "## Daily Log: July 12, 2026\n\n### Tools & Tech Encountered\n"
                        "- **None:** [No qualifying items for this section]")

    assert dc.main() == 0
    assert stubbed_run["persists"] == []
    assert any(_is_run_success(line) for line in capsys.readouterr().out.splitlines())


def test_fetch_failure_is_a_failed_run(stubbed_run, monkeypatch):
    calls = []
    monkeypatch.setattr(dc, "fetch_chrome_history",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sqlite boom")))
    monkeypatch.setattr(dc, "notify_failure", lambda name, detail, logger=None: calls.append(str(detail)))
    assert dc.main() == 1
    assert any("sqlite boom" in c for c in calls)
    assert stubbed_run["persists"] == []  # never reached the write
