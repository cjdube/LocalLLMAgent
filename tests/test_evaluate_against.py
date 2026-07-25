"""Tests for agent/tools/evaluate_against.py — the lens loads from the wiki, the
target resolves from a URL or inline text, the degrade contracts (missing lens,
lens-load error, fetch error, no target all short-circuit before the model), and
the model sees both the lens and the target. read_wiki_page, fetch_webpage, and
complete_text are monkeypatched — no vault, no network, no model."""

import pytest

from agent.tools import evaluate_against as eg


@pytest.fixture
def stubbed_pipeline(monkeypatch):
    seen = {"prompts": []}
    monkeypatch.setattr(eg, "read_wiki_page",
                        lambda name: {"content": "# Product Principles\n\nShip small. Own the outcome."})
    monkeypatch.setattr(eg, "fetch_webpage",
                        lambda url: {"url": url, "title": "Quorum",
                                     "markdown": "# Quorum\n\nA [product](https://q.com) page."})
    monkeypatch.setattr(eg, "complete_text",
                        lambda **k: seen["prompts"].append(k) or "## Where It Aligns\nShips small.")
    return seen


# --------------------------------------------------------------------------- #
# happy paths
# --------------------------------------------------------------------------- #

def test_url_target_returns_evaluation(stubbed_pipeline):
    out = eg.evaluate_against("product-principles", target_url="https://quorum.example")
    assert out == {"lens": "product-principles",
                   "evaluation": "## Where It Aligns\nShips small."}
    prompt = stubbed_pipeline["prompts"][0]["user_prompt"]
    # The model saw Craig's standards (the lens) AND the compacted target (link
    # text kept, target dropped).
    assert "Ship small" in prompt and "Own the outcome" in prompt
    assert "https://quorum.example" in prompt
    assert "product" in prompt and "https://q.com" not in prompt


def test_text_target_skips_fetch(stubbed_pipeline, monkeypatch):
    # A URL fetch here would be a bug: text input must not touch the network.
    monkeypatch.setattr(eg, "fetch_webpage",
                        lambda url: (_ for _ in ()).throw(AssertionError("must not fetch")))
    out = eg.evaluate_against("product-principles", target_text="our pitch is fast and cheap")
    assert out["lens"] == "product-principles"
    assert "our pitch is fast and cheap" in stubbed_pipeline["prompts"][0]["user_prompt"]


# --------------------------------------------------------------------------- #
# degrade contracts — each short-circuits before any model call
# --------------------------------------------------------------------------- #

def test_missing_lens_page_is_an_error(stubbed_pipeline):
    out = eg.evaluate_against("", target_url="https://quorum.example")
    assert "error" in out
    assert stubbed_pipeline["prompts"] == []


def test_lens_load_error_propagates(stubbed_pipeline, monkeypatch):
    monkeypatch.setattr(eg, "read_wiki_page", lambda name: {"error": "wiki page 'x' not found"})
    out = eg.evaluate_against("x", target_url="https://quorum.example")
    assert "not found" in out["error"]
    assert stubbed_pipeline["prompts"] == []


def test_empty_lens_is_an_error(stubbed_pipeline, monkeypatch):
    monkeypatch.setattr(eg, "read_wiki_page", lambda name: {"content": "   \n\n"})
    out = eg.evaluate_against("blank", target_text="anything")
    assert "empty" in out["error"]
    assert stubbed_pipeline["prompts"] == []


def test_fetch_error_propagates(stubbed_pipeline, monkeypatch):
    monkeypatch.setattr(eg, "fetch_webpage", lambda url: {"error": "HTTP 402: out of credits"})
    out = eg.evaluate_against("product-principles", target_url="https://quorum.example")
    assert "HTTP 402" in out["error"]
    assert stubbed_pipeline["prompts"] == []


def test_no_target_is_an_error(stubbed_pipeline):
    out = eg.evaluate_against("product-principles")
    assert "error" in out
    assert stubbed_pipeline["prompts"] == []


def test_model_exception_is_caught_not_raised(stubbed_pipeline, monkeypatch):
    def boom(**_):
        raise RuntimeError("ollama down")
    monkeypatch.setattr(eg, "complete_text", boom)
    out = eg.evaluate_against("product-principles", target_text="anything")
    assert "error" in out and "ollama down" in out["error"]


def test_empty_model_output_is_an_error_not_a_blank_evaluation(stubbed_pipeline, monkeypatch):
    # A thinking model that spends its whole num_predict budget reasoning emits
    # no content (observed: 1 run in 3 before think=False). Returning that as an
    # evaluation hands the user a blank answer with nothing to act on.
    monkeypatch.setattr(eg, "complete_text", lambda **k: "  \n ")

    out = eg.evaluate_against("product-principles", target_text="some target")

    assert "error" in out and "empty evaluation" in out["error"]
    assert "evaluation" not in out


def test_judging_does_not_spend_the_budget_on_thinking(stubbed_pipeline):
    eg.evaluate_against("product-principles", target_text="some target")

    assert stubbed_pipeline["prompts"][0]["think"] is False
