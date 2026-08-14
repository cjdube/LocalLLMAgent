"""Tests for agent/tools/evaluate_app.py — the compaction rules, the degrade
contracts (fetch failure propagates, a model exception is caught), and that
the model sees the compacted page. fetch_webpage and complete_text are
monkeypatched — no network, no model."""

import pytest

from agent.tools import evaluate_app as ea


@pytest.fixture
def stubbed_pipeline(monkeypatch):
    seen = {"prompts": []}
    monkeypatch.setattr(ea, "fetch_webpage",
                        lambda url: {"url": url, "title": "Quorum",
                                     "markdown": "# Quorum\n\nA [product](https://q.com) page."})
    monkeypatch.setattr(ea, "complete_text",
                        lambda **k: seen["prompts"].append(k) or "## Overall Assessment\nRisky.")
    return seen


# --------------------------------------------------------------------------- #
# _compact
# --------------------------------------------------------------------------- #

def test_compact_strips_images_and_link_targets():
    md = "![logo](https://x/logo.png)\nSee [the docs](https://x/docs) here."
    out = ea._compact(md)
    assert "https://x" not in out
    assert "the docs" in out


def test_compact_collapses_whitespace(monkeypatch):
    out = ea._compact("a  b\t c\n\n\n\n\nd" + "x" * 100)
    assert "\n\n\n" not in out and "  " not in out


def test_compact_does_not_bound_length_itself(monkeypatch):
    # Bounding belongs to the caller. When _compact capped internally it
    # outranked evaluate_against's larger bound two modules away, silently
    # cutting the lens it promises to pass whole.
    monkeypatch.setattr(ea, "_CONTENT_CHARS", 20)

    assert len(ea._compact("x" * 100)) == 100


def test_the_pipeline_bounds_the_page_at_content_chars(stubbed_pipeline, monkeypatch):
    monkeypatch.setattr(ea, "_CONTENT_CHARS", 20)
    monkeypatch.setattr(ea, "fetch_webpage",
                        lambda url, **k: {"url": url, "title": "T", "markdown": "x" * 100})

    ea.evaluate_app("https://quorum.example")

    assert "x" * 20 in stubbed_pipeline["prompts"][0]["user_prompt"]
    assert "x" * 21 not in stubbed_pipeline["prompts"][0]["user_prompt"]


# --------------------------------------------------------------------------- #
# evaluate_app contracts
# --------------------------------------------------------------------------- #

def test_happy_path_returns_teardown(stubbed_pipeline):
    out = ea.evaluate_app("https://quorum.example")
    assert out == {"url": "https://quorum.example",
                   "teardown": "## Overall Assessment\nRisky."}
    prompt = stubbed_pipeline["prompts"][0]["user_prompt"]
    # The model saw the url, the title, and the compacted copy (link target gone).
    assert "https://quorum.example" in prompt and "Quorum" in prompt
    assert "product" in prompt and "https://q.com" not in prompt


def test_fetch_error_propagates_without_model_call(stubbed_pipeline, monkeypatch):
    monkeypatch.setattr(ea, "fetch_webpage", lambda url: {"error": "HTTP 402: out of credits"})
    out = ea.evaluate_app("https://quorum.example")
    assert "HTTP 402" in out["error"]
    assert stubbed_pipeline["prompts"] == []


def test_content_empty_after_compaction_is_an_error(stubbed_pipeline, monkeypatch):
    monkeypatch.setattr(ea, "fetch_webpage",
                        lambda url: {"url": url, "title": "", "markdown": "![x](https://x.png)"})
    out = ea.evaluate_app("https://quorum.example")
    assert "error" in out
    assert stubbed_pipeline["prompts"] == []


def test_model_exception_is_caught_not_raised(stubbed_pipeline, monkeypatch):
    def boom(**_):
        raise RuntimeError("ollama down")
    monkeypatch.setattr(ea, "complete_text", boom)
    out = ea.evaluate_app("https://quorum.example")
    assert "error" in out and "ollama down" in out["error"]


def test_empty_model_output_is_an_error_not_a_blank_teardown(stubbed_pipeline, monkeypatch):
    # Backstop for the truncation that hit evaluate_against: a blank teardown is
    # indistinguishable from a real one to a caller that only checks for "error".
    monkeypatch.setattr(ea, "complete_text", lambda **k: "")

    out = ea.evaluate_app("https://quorum.example")

    assert "error" in out and "empty teardown" in out["error"]
    assert "teardown" not in out
