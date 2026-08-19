"""Tests for agent/tools/web_search.py — input clamping/validation and the
Tavily response parse. The network POST is stubbed; per the live-API precedent
only the network-free logic is exercised."""

import json

import requests

from agent.tools import web_search


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _capture_post(monkeypatch, box, payload=None):
    """Stub requests.post to record the request body Tavily would receive."""
    def fake_post(url, json=None, timeout=None):
        box.clear()
        box.update(json)
        return _Resp(payload if payload is not None else {"results": []})
    monkeypatch.setattr(web_search.requests, "post", fake_post)


def test_max_results_clamped_to_ten(monkeypatch):
    box = {}
    _capture_post(monkeypatch, box)
    web_search.search_web("q", max_results=999, api_key="k")
    assert box["max_results"] == 10


def test_max_results_floor_is_one(monkeypatch):
    box = {}
    _capture_post(monkeypatch, box)
    web_search.search_web("q", max_results=-5, api_key="k")
    assert box["max_results"] == 1


def test_invalid_topic_coerced_to_general(monkeypatch):
    box = {}
    _capture_post(monkeypatch, box)
    web_search.search_web("q", topic="sports", api_key="k")
    assert box["topic"] == "general"


def test_days_dropped_for_general_topic(monkeypatch):
    box = {}
    _capture_post(monkeypatch, box)
    web_search.search_web("q", topic="general", days=3, api_key="k")
    assert "days" not in box  # Tavily only honors days for news


def test_days_applied_and_floored_for_news(monkeypatch):
    box = {}
    _capture_post(monkeypatch, box)
    web_search.search_web("q", topic="news", days=-2, api_key="k")
    assert box["days"] == 1


def test_empty_query_short_circuits_without_calling_the_api(monkeypatch):
    calls = []
    monkeypatch.setattr(web_search.requests, "post", lambda *a, **k: calls.append(1))
    out = web_search.search_web("   ", api_key="k")
    assert "error" in out and not calls


def test_missing_key_short_circuits(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    out = web_search.search_web("q", api_key=None)
    assert "TAVILY_API_KEY" in out["error"]


def test_network_error_maps_uniformly(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("refused")
    monkeypatch.setattr(web_search.requests, "post", boom)
    out = web_search.search_web("q", api_key="k")
    assert out["error"].startswith("network error")


def test_parse_extracts_results_and_answer():
    raw = {"results": [{"title": "T", "url": "u", "content": "c",
                        "published_date": "2026-01-01"}],
           "answer": "the answer"}
    out = web_search._parse(raw)
    assert out["answer"] == "the answer"
    assert out["results"][0] == {"title": "T", "url": "u", "content": "c",
                                 "published_date": "2026-01-01"}


def test_parse_omits_answer_when_absent():
    out = web_search._parse({"results": []})
    assert "answer" not in out and out["results"] == []


# --- bounding the payload ----------------------------------------------------
# max_results caps the result COUNT, never the size of one, so ten page
# scrapings was unbounded and the agent loop's blind backstop was the only thing
# holding it. It fired on an ordinary 5-result news search and ate the tail of
# `answer`, which used to come last.

def test_an_oversized_snippet_is_trimmed_and_says_so():
    long_content = "x" * (web_search.MAX_CONTENT_CHARS + 500)
    out = web_search._parse({"results": [{"title": "T", "url": "u",
                                          "content": long_content}]})

    content = out["results"][0]["content"]
    assert content.startswith("x" * web_search.MAX_CONTENT_CHARS)
    assert content.endswith("[trimmed]")


def test_a_snippet_within_budget_is_untouched():
    content = "y" * web_search.MAX_CONTENT_CHARS
    out = web_search._parse({"results": [{"title": "T", "url": "u",
                                          "content": content}]})

    assert out["results"][0]["content"] == content


def test_the_answer_leads_so_a_trim_cannot_take_it():
    raw = {"results": [{"title": "T", "url": "u", "content": "c"}],
           "answer": "the answer"}

    keys = list(web_search._parse(raw))
    assert keys.index("answer") < keys.index("results")


def test_a_typical_news_search_is_not_trimmed_at_all():
    # The overrun that started this was an ordinary 5-result news search whose
    # snippets ran ~1900 chars. That case must now pass through whole.
    raw = {"answer": "The Red Sox beat the Diamondbacks 11-1.",
           "results": [{"title": f"Recap {i}", "url": f"https://espn.com/{i}",
                        "content": "c" * 1100, "published_date": "2026-08-18"}
                       for i in range(5)]}

    out = web_search._parse(raw)
    assert len(out["results"]) == 5
    assert "results_omitted" not in out
    assert out["answer"].endswith("11-1.")


def test_whole_results_are_dropped_rather_than_one_sliced():
    raw = {"results": [{"title": f"T{i}", "url": f"u{i}", "content": "c" * 1200,
                        "published_date": "2026-01-01"} for i in range(10)]}

    out = web_search._parse(raw)

    assert len(out["results"]) < 10
    # Every result that survived is intact — no half-written trailing record.
    for r in out["results"]:
        assert set(r) == {"title", "url", "content", "published_date"}
    assert "10" in out["results_omitted"]


def test_the_worst_case_stays_inside_the_loop_backstop():
    # The number in loop.TOOL_RESULT_CHAR_CAPS is only honest if the tool's own
    # budget actually holds the worst case underneath it. Every field here is
    # remote text, so every field is oversized.
    from agent import loop

    raw = {"answer": "a" * 20000,
           "results": [{"title": "T" * 2000, "url": "https://example.com/" + "u" * 2000,
                        "content": "c" * 20000, "published_date": "d" * 500}
                       for _ in range(10)]}

    payload = json.dumps(web_search._parse(raw))
    assert len(payload) <= loop.TOOL_RESULT_CHAR_CAPS["search_web"]
