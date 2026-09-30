"""Tests for agent/tools/shopping.py — validation, the degrade contracts (one
dead source still produces picks; everything dead is an error), the score
parse's WARNING on a partial/zero parse, the price-only fallback, dedupe, and
that the negotiation kit's numbers land right. No network: _shop_sources and
search_web are monkeypatched; complete_text is stubbed per test."""

import logging

import pytest

from agent.tools import shopping as sh
from agent.tools import _shop_sources


def _offer(title, price, source="ebay", shipping=0.0, condition="New",
           url=None, best_offer=False, regular_price=None, seller="seller1"):
    total = price + shipping if shipping is not None else None
    return {
        "title": title, "price": price, "shipping": shipping, "total": total,
        "seller": seller, "source": source, "condition": condition,
        "url": url or f"https://example.com/{title.replace(' ', '-')}",
        "best_offer": best_offer, "regular_price": regular_price,
    }


@pytest.fixture(autouse=True)
def _no_search_web(monkeypatch):
    # Default stub for the review-context search; individual tests override.
    monkeypatch.setattr(sh, "search_web", lambda *a, **k: {"answer": None, "results": []})


def _stub_sources(monkeypatch, ebay_offers=(), bestbuy_offers=(), ebay_error=None,
                   bestbuy_error=None):
    def ebay(query, max_price, limit=10):
        if ebay_error:
            return {"error": ebay_error}
        return {"offers": list(ebay_offers)}

    def bestbuy(query, max_price, limit=10):
        if bestbuy_error:
            return {"error": bestbuy_error}
        return {"offers": list(bestbuy_offers)}

    monkeypatch.setattr(_shop_sources, "search_ebay", ebay)
    monkeypatch.setattr(_shop_sources, "search_bestbuy", bestbuy)


def _stub_model(monkeypatch, query_response="wireless headphones\nnoise cancelling headphones",
                 score_response=None, calls=None):
    calls = calls if calls is not None else []

    def fake(**kwargs):
        calls.append(kwargs)
        # First call is the query step, second is the score step.
        if len(calls) == 1:
            return query_response
        return score_response if score_response is not None else ""

    monkeypatch.setattr(sh, "complete_text", fake)
    return calls


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", [0, -5, "abc", None])
def test_bad_max_price_is_rejected(bad):
    result = sh.shop("a widget", bad)
    assert result == {"error": "max_price must be a positive number"}


def test_dollar_and_comma_price_is_accepted(monkeypatch):
    _stub_sources(monkeypatch, ebay_offers=[_offer("Widget", 100.0)])
    _stub_model(monkeypatch, score_response="1|9|great fit")
    result = sh.shop("a widget", "$1,200")
    assert result["max_price"] == 1200.0


def test_empty_description_is_rejected():
    assert sh.shop("   ", 100) == {"error": "description was empty"}


# --------------------------------------------------------------------------- #
# happy path / ranking / negotiation kit
# --------------------------------------------------------------------------- #

def test_happy_path_ranks_by_score_then_price(monkeypatch):
    cheap_low_score = _offer("Cheap Bad Fit", 50.0, url="https://x/1")
    pricier_good_fit = _offer("Great Fit Headphones", 80.0, url="https://x/2")
    cheapest_good_fit = _offer("Best Fit Headphones", 60.0, url="https://x/3")
    _stub_sources(monkeypatch, ebay_offers=[cheap_low_score, pricier_good_fit,
                                             cheapest_good_fit])
    # n=1 cheap_low_score, n=2 pricier_good_fit, n=3 cheapest_good_fit (sorted by price first)
    score_lines = "1|3|missing feature\n2|9|great match\n3|9|great match"
    calls = _stub_model(monkeypatch, score_response=score_lines)

    result = sh.shop("noise cancelling headphones", 150, must_haves="over-ear")

    assert "error" not in result
    assert result["ranking"] == "fit_then_price"
    picks = result["picks"]
    # Both score-9 items beat the score-3 item; cheapest of the two ties wins first.
    assert picks[0]["title"] == "Best Fit Headphones"
    assert picks[1]["title"] == "Great Fit Headphones"
    assert all(p["url"] for p in picks)
    assert len(calls) == 2


def test_best_offer_item_gets_offer_message_with_rounded_price(monkeypatch):
    offer = _offer("Some Gadget", 129.99, best_offer=True, url="https://x/1")
    _stub_sources(monkeypatch, ebay_offers=[offer])
    _stub_model(monkeypatch, score_response="1|8|good match")

    result = sh.shop("some gadget", 200)
    pick = result["picks"][0]
    assert pick["suggested_offer"] == 110
    assert pick["offer_message"] is not None
    assert "$110" in pick["offer_message"]
    # The draft is what Craig copies to the seller, so it must reach the text
    # he reads — the job result — not only the structured pick.
    assert pick["offer_message"] in result["summary"]
    assert result["headline"].startswith("1 option(s)")


def test_best_offer_small_price_rounds_to_whole_dollar(monkeypatch):
    offer = _offer("Cheap Gadget", 47.50, best_offer=True, url="https://x/1")
    _stub_sources(monkeypatch, ebay_offers=[offer])
    _stub_model(monkeypatch, score_response="1|8|good match")

    result = sh.shop("cheap gadget", 100)
    assert result["picks"][0]["suggested_offer"] == 40


# --------------------------------------------------------------------------- #
# scoring prompt hygiene
# --------------------------------------------------------------------------- #

def test_score_prompt_has_no_urls_or_seller_names(monkeypatch):
    offer = _offer("Widget", 50.0, url="https://secret.example/listing/123",
                    seller="sneaky_seller_99")
    _stub_sources(monkeypatch, ebay_offers=[offer])
    calls = _stub_model(monkeypatch, score_response="1|9|fits")

    sh.shop("a widget", 100)

    score_prompt = calls[1]["user_prompt"]
    assert "http" not in score_prompt
    assert "sneaky_seller_99" not in score_prompt


def test_every_complete_text_call_has_think_false_and_logger(monkeypatch):
    offer = _offer("Widget", 50.0, url="https://x/1")
    _stub_sources(monkeypatch, ebay_offers=[offer])
    calls = _stub_model(monkeypatch, score_response="1|9|fits")

    sh.shop("a widget", 100)

    assert len(calls) == 2
    for call in calls:
        assert call["think"] is False
        assert call["logger"] is sh.logger


# --------------------------------------------------------------------------- #
# score-parse degrade paths
# --------------------------------------------------------------------------- #

def test_partial_score_parse_logs_warning_with_counts(monkeypatch, caplog):
    offers = [_offer(f"Item {i}", 10.0 * i, url=f"https://x/{i}") for i in range(1, 5)]
    _stub_sources(monkeypatch, ebay_offers=offers)
    # Only 2 of 4 candidates scored.
    _stub_model(monkeypatch, score_response="1|9|great\n2|8|great")

    with caplog.at_level(logging.WARNING, logger="wren"):
        result = sh.shop("some item", 100)

    assert "error" not in result
    assert any("2 of 4" in r.message for r in caplog.records)


def test_zero_parsed_scores_falls_back_to_price_only(monkeypatch, caplog):
    offers = [_offer(f"Item {i}", 10.0 * i, url=f"https://x/{i}") for i in range(1, 4)]
    _stub_sources(monkeypatch, ebay_offers=offers)
    _stub_model(monkeypatch, score_response="garbage, no pipes here")

    with caplog.at_level(logging.WARNING, logger="wren"):
        result = sh.shop("some item", 100)

    assert result["ranking"] == "price_only"
    assert len(result["picks"]) == 3
    # Cheapest first.
    assert [p["price"] for p in result["picks"]] == [10.0, 20.0, 30.0]


# --------------------------------------------------------------------------- #
# source errors
# --------------------------------------------------------------------------- #

def test_one_dead_source_still_produces_picks_and_names_the_error(monkeypatch):
    offer = _offer("Widget", 50.0, url="https://x/1")
    _stub_sources(monkeypatch, ebay_offers=[offer], bestbuy_error="bad api key")
    _stub_model(monkeypatch, score_response="1|9|fits")

    result = sh.shop("a widget", 100)

    assert "error" not in result
    assert len(result["picks"]) == 1
    assert any("bestbuy" in e and "bad api key" in e for e in result["source_errors"])


def test_both_sources_dead_or_empty_is_an_error(monkeypatch):
    _stub_sources(monkeypatch, ebay_error="down", bestbuy_error="down")
    calls = _stub_model(monkeypatch)

    result = sh.shop("a widget", 100)

    assert "error" in result
    assert "source_errors" in result
    # No candidates -> no score call needed, only the query call.
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# query fallback
# --------------------------------------------------------------------------- #

def test_garbage_query_response_falls_back_to_description(monkeypatch, caplog):
    offer = _offer("Widget", 50.0, url="https://x/1")
    _stub_sources(monkeypatch, ebay_offers=[offer])
    _stub_model(monkeypatch, query_response="!!\nx\n", score_response="1|9|fits")

    with caplog.at_level(logging.WARNING, logger="wren"):
        result = sh.shop("a very specific widget description", 100)

    assert result["queries"] == ["a very specific widget description"[:80]]
    assert any("using the description" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# model exception propagates
# --------------------------------------------------------------------------- #

def test_model_exception_propagates(monkeypatch):
    def boom(**k):
        raise RuntimeError("ollama not running")
    monkeypatch.setattr(sh, "complete_text", boom)

    with pytest.raises(RuntimeError):
        sh.shop("a widget", 100)


# --------------------------------------------------------------------------- #
# dedupe
# --------------------------------------------------------------------------- #

def test_dedupe_on_url(monkeypatch):
    same_url_offer_a = _offer("Widget", 50.0, url="https://x/1")
    same_url_offer_b = _offer("Widget", 50.0, url="https://x/1")
    _stub_sources(monkeypatch, ebay_offers=[same_url_offer_a],
                  bestbuy_offers=[same_url_offer_b])
    _stub_model(monkeypatch, score_response="1|9|fits")

    result = sh.shop("a widget", 100)

    assert result["candidates_considered"] == 1


# --------------------------------------------------------------------------- #
# the chat tool
# --------------------------------------------------------------------------- #

def test_start_shopping_queues_a_shopping_job(monkeypatch, tmp_path):
    from agent.tools import background
    monkeypatch.setattr(background, "_STORE_PATH", tmp_path / "bg_jobs.json")
    monkeypatch.setattr(_shop_sources, "configured_sources", lambda: ["ebay"])

    result = sh.start_shopping(description="standing desk", max_price="$300",
                               must_haves="memory presets")

    assert result["status"] == "pending"
    assert result["tool_name"] == "start_shopping"
    job = background.next_actionable()
    assert job["kind"] == "shopping"
    assert job["params"] == {"description": "standing desk", "max_price": 300.0,
                             "must_haves": "memory presets"}
    assert "under $300" in job["task_text"]


@pytest.mark.parametrize("bad", [None, 0, "lots"])
def test_start_shopping_rejects_a_bad_budget_before_queueing(monkeypatch, tmp_path, bad):
    from agent.tools import background
    monkeypatch.setattr(background, "_STORE_PATH", tmp_path / "bg_jobs.json")

    assert "error" in sh.start_shopping(description="desk", max_price=bad)
    assert background.next_actionable() is None


def test_start_shopping_refuses_when_no_store_has_a_key(monkeypatch, tmp_path):
    from agent.tools import background
    monkeypatch.setattr(background, "_STORE_PATH", tmp_path / "bg_jobs.json")
    monkeypatch.setattr("agent.tools._shop_sources.resolve_key", lambda name, arg=None: None)

    result = sh.start_shopping(description="desk", max_price=300)

    assert "BESTBUY_API_KEY" in result["error"]
    assert background.next_actionable() is None


@pytest.mark.parametrize("keys, expected", [
    ({"EBAY_CLIENT_ID": "i", "EBAY_CLIENT_SECRET": "s"}, ["ebay"]),
    ({"EBAY_CLIENT_ID": "i"}, []),  # half an eBay keyset cannot get a token
    ({"BESTBUY_API_KEY": "k"}, ["bestbuy"]),
])
def test_configured_sources_needs_every_key_of_a_source(monkeypatch, keys, expected):
    monkeypatch.setattr("agent.tools._shop_sources.resolve_key",
                        lambda name, arg=None: keys.get(name))
    assert _shop_sources.configured_sources() == expected


def test_start_shopping_is_gated_and_never_offered_to_a_background_job():
    from agent import toolset
    assert "start_shopping" in toolset.WRITE_TOOLS
    assert "start_shopping" in toolset.UNATTENDED_EXCLUDED_TOOLS
    card = toolset.describe_call({"function": {"name": "start_shopping", "arguments": {
        "description": "standing desk", "max_price": 300}}})
    assert card == 'Shop in the background for "standing desk" under $300'
