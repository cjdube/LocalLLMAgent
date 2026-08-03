"""Tests for tasks/project_scan.py — main() distils each documented project into
a summary + topics, caches on content_hash, skips unchanged projects, prunes
deleted ones, and reports the two silent degradations (a project with no docs,
and a distillation that comes back too thin to match anything).

Collaborators are monkeypatched; no model access. The registry store is
redirected to tmp_path by conftest.
"""

import logging

import pytest

from agent.store import atomic_write_json, load_json
from tasks import project_scan as ps


@pytest.fixture
def stub(monkeypatch):
    calls = {"complete": [], "kwargs": []}

    # setup_logger builds a logger with propagate=False (so a task's output stays
    # in its own file), which caplog cannot see. Swap in a propagating one — the
    # WARNINGs are half of what this task's contract is, so they need asserting.
    def _logger(task_name):
        logger = logging.getLogger(f"test_{task_name}")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        logger.propagate = True
        return logger
    monkeypatch.setattr(ps, "setup_logger", _logger)

    monkeypatch.setattr(ps, "resolve_backend", lambda key: None)
    monkeypatch.setattr(ps, "warm_model", lambda **k: True)
    monkeypatch.setattr(ps, "notify_failure", lambda *a, **k: None)

    def _complete_text(system_prompt, user_prompt, **k):
        calls["complete"].append(user_prompt)
        calls["kwargs"].append(k)
        return "summary: A thing that does a thing.\ntopics: sse, ollama, sqlite, flask\n"
    monkeypatch.setattr(ps, "complete_text", _complete_text)
    return calls


def _row(name, **over):
    row = {"name": name, "path": f"/p/{name}", "readme": f"# {name}\nDoes things.",
           "claude_md": "", "doc_titles": [], "content_hash": f"hash-{name}",
           "remote": None, "branch": None, "last_commit": None,
           "commits_30d": None, "dirty": None}
    row.update(over)
    return row


def _scan(monkeypatch, *rows):
    monkeypatch.setattr(ps, "scan_projects", lambda: {"projects": list(rows)})


def _stored():
    return {p["name"]: p for p in load_json(ps.PROJECTS_PATH, {}).get("projects", [])}


# --- distillation parsing ---------------------------------------------------

def test_parse_distillation_reads_both_lines():
    parsed = ps.parse_distillation(
        "summary: A local agent.\ntopics: Ollama, SSE, launchd\n")
    assert parsed["summary"] == "A local agent."
    assert parsed["topics"] == ["ollama", "sse", "launchd"]


def test_parse_distillation_tolerates_preamble_and_dupes():
    parsed = ps.parse_distillation(
        "Here you go:\n\nSummary:  A local agent.  \nTopics: sse, SSE, sse.\n")
    assert parsed["summary"] == "A local agent."
    assert parsed["topics"] == ["sse"]


def test_parse_distillation_of_empty_output_is_empty_not_an_exception():
    # The failure mode CLAUDE.md pins: a call that reasons too long returns
    # empty content, not a truncated answer.
    assert ps.parse_distillation("") == {"summary": "", "topics": []}
    assert ps.parse_distillation(None) == {"summary": "", "topics": []}


def test_parse_distillation_caps_topics(monkeypatch):
    monkeypatch.setattr(ps, "MAX_TOPICS", 3)
    parsed = ps.parse_distillation("summary: x\ntopics: a, b, c, d, e, f")
    assert parsed["topics"] == ["a", "b", "c"]


# --- main() -----------------------------------------------------------------

def test_distils_each_documented_project_once(stub, monkeypatch):
    _scan(monkeypatch, _row("Alpha"), _row("Beta"))

    assert ps.main([]) == 0
    stored = _stored()
    assert stored["Alpha"]["summary"] == "A thing that does a thing."
    assert stored["Alpha"]["topics"] == ["sse", "ollama", "sqlite", "flask"]
    # One isolated call per project — project count drives the number of calls,
    # not the size of any prompt.
    assert len(stub["complete"]) == 2


def test_passes_think_false_and_a_logger(stub, monkeypatch):
    # A fixed two-line template: thinking tokens share the num_predict budget, so
    # leaving it on returns empty content (CLAUDE.md). The logger is what surfaces
    # loop.py's cut-off warning.
    _scan(monkeypatch, _row("Alpha"))

    assert ps.main([]) == 0
    assert stub["kwargs"][0]["think"] is False
    assert stub["kwargs"][0]["logger"] is not None


def test_the_prompt_carries_readme_claude_md_and_doc_titles(stub, monkeypatch):
    _scan(monkeypatch, _row("Alpha", claude_md="House rules.",
                            doc_titles=["Design", "Security"]))

    assert ps.main([]) == 0
    prompt = stub["complete"][0]
    assert "Does things." in prompt and "House rules." in prompt
    assert "Design, Security" in prompt


def test_skips_projects_whose_docs_have_not_changed(stub, monkeypatch):
    _scan(monkeypatch, _row("Alpha"))
    assert ps.main([]) == 0
    assert len(stub["complete"]) == 1

    # Second run, same content_hash: the git facts refresh, the model does not run.
    _scan(monkeypatch, _row("Alpha", last_commit="2026-08-03", dirty=True))
    assert ps.main([]) == 0
    assert len(stub["complete"]) == 1
    stored = _stored()["Alpha"]
    assert stored["last_commit"] == "2026-08-03" and stored["dirty"] is True
    assert stored["summary"] == "A thing that does a thing."


def test_changed_docs_trigger_a_redistillation(stub, monkeypatch):
    _scan(monkeypatch, _row("Alpha"))
    assert ps.main([]) == 0

    _scan(monkeypatch, _row("Alpha", content_hash="hash-Alpha-v2"))
    assert ps.main([]) == 0
    assert len(stub["complete"]) == 2


def test_refresh_regenerates_everything(stub, monkeypatch):
    _scan(monkeypatch, _row("Alpha"))
    assert ps.main([]) == 0
    assert ps.main(["--refresh"]) == 0
    assert len(stub["complete"]) == 2


def test_a_project_with_no_docs_gets_a_row_but_no_blurb(stub, monkeypatch, caplog):
    # AgenticOS, my-agent-hq, AIChatScraper and SortOfCardGame are all in this
    # state on the real machine. They must not silently vanish: the reason they
    # will never surface in a nudge is that they have no README to read.
    _scan(monkeypatch, _row("Empty", readme="", claude_md="", doc_titles=[]))

    with caplog.at_level(logging.WARNING):
        assert ps.main([]) == 0
    assert stub["complete"] == []
    stored = _stored()["Empty"]
    assert stored["summary"] == "" and stored["topics"] == []
    assert "no README" in caplog.text and "Empty" in caplog.text


def test_reports_a_distillation_too_thin_to_match(stub, monkeypatch, caplog):
    # The silent degradation this task is prone to: the project still appears and
    # still has a summary, it just quietly stops matching anything.
    monkeypatch.setattr(ps, "complete_text",
                        lambda **k: "summary: A thing.\ntopics: sse\n")
    _scan(monkeypatch, _row("Alpha"))

    with caplog.at_level(logging.WARNING):
        assert ps.main([]) == 0
    assert "fewer than 4 topics" in caplog.text and "Alpha" in caplog.text


def test_falls_back_to_the_readme_when_the_model_returns_nothing(stub, monkeypatch, caplog):
    monkeypatch.setattr(ps, "complete_text", lambda **k: "")
    _scan(monkeypatch, _row("Alpha", readme="# Alpha\nThe first real line."))

    with caplog.at_level(logging.WARNING):
        assert ps.main([]) == 0
    # The heading is stripped, so the fallback is the line that says something.
    assert _stored()["Alpha"]["summary"] == "Alpha"
    assert "no usable summary" in caplog.text


def test_a_project_whose_blurb_never_landed_is_retried(stub, monkeypatch):
    # An empty summary in the cache is a failed run, not a cache hit — without
    # this the first bad distillation would be frozen in forever.
    atomic_write_json(ps.PROJECTS_PATH, {"projects": [
        {"name": "Alpha", "content_hash": "hash-Alpha", "summary": "", "topics": []}]})
    _scan(monkeypatch, _row("Alpha"))

    assert ps.main([]) == 0
    assert len(stub["complete"]) == 1


def test_deleted_projects_are_pruned(stub, monkeypatch):
    _scan(monkeypatch, _row("Alpha"), _row("Beta"))
    assert ps.main([]) == 0
    assert set(_stored()) == {"Alpha", "Beta"}

    _scan(monkeypatch, _row("Alpha"))
    assert ps.main([]) == 0
    assert set(_stored()) == {"Alpha"}


def test_the_store_does_not_carry_the_document_bodies(stub, monkeypatch):
    # The registry is read every morning; there is no reason to persist 2000
    # chars of README per project once it has been distilled.
    _scan(monkeypatch, _row("Alpha", claude_md="House rules."))

    assert ps.main([]) == 0
    assert "readme" not in _stored()["Alpha"]
    assert "claude_md" not in _stored()["Alpha"]


def test_a_failing_scan_notifies_and_exits_nonzero(stub, monkeypatch):
    sent = []
    monkeypatch.setattr(ps, "notify_failure", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(ps, "scan_projects", lambda: {"error": "projects dir not found"})

    assert ps.main([]) == 1
    assert sent


def test_load_projects_without_a_store_is_empty(monkeypatch):
    assert ps.load_projects() == []
