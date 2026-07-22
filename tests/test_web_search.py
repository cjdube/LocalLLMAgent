"""Tests for agent/tools/web_search.py — input clamping/validation and the
Tavily response parse. The network POST is stubbed; per the live-API precedent
only the network-free logic is exercised."""

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
