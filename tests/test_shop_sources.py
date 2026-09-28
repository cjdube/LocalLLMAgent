"""Tests for agent/tools/_shop_sources.py — input clamping/validation, offer
normalization, the eBay token cache, and Best Buy query sanitization. Network
calls are stubbed at the module's own _http_get/_http_post seam (never
requests.get/post directly — tests/conftest.py's suite-wide network guard
patches those same two names, so patching `requests` here would be
overwritten by that guard rather than actually stubbing anything)."""

import pathlib

import pytest

from agent.tools import _shop_sources as shop


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(f"{self.status_code} error", response=self)

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _clear_token_cache():
    """The eBay token cache is module-level and would otherwise leak a
    'token' across tests."""
    shop._ebay_token_cache.clear()
    yield
    shop._ebay_token_cache.clear()


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("EBAY_CLIENT_ID", "id123")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret123")
    monkeypatch.setenv("BESTBUY_API_KEY", "bbkey123")


EBAY_ITEM = {
    "title": "Widget Pro",
    "price": {"value": "49.99", "currency": "USD"},
    "shippingOptions": [{"shippingCost": {"value": "5.00"}}],
    "seller": {"username": "widgetco"},
    "condition": "New",
    "itemWebUrl": "https://ebay.com/itm/123",
    "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"],
}

BESTBUY_ITEM = {
    "name": "Gadget X",
    "salePrice": 199.99,
    "regularPrice": 249.99,
    "url": "https://bestbuy.com/site/123.p",
    "shippingCost": 0.0,
    "freeShipping": True,
    "condition": "new",
}


def test_ebay_happy_path(monkeypatch):
    monkeypatch.setattr(
        shop, "_http_post",
        lambda url, **kw: _Resp({"access_token": "tok", "expires_in": 3600}),
    )
    monkeypatch.setattr(
        shop, "_http_get",
        lambda url, **kw: _Resp({"itemSummaries": [EBAY_ITEM]}),
    )
    result = shop.search_ebay("widget", 100.0)
    assert "error" not in result
    assert len(result["offers"]) == 1
    offer = result["offers"][0]
    assert offer["title"] == "Widget Pro"
    assert offer["price"] == 49.99
    assert offer["shipping"] == 5.00
    assert offer["total"] == 54.99
    assert offer["seller"] == "widgetco"
    assert offer["source"] == "ebay"
    assert offer["condition"] == "New"
    assert offer["url"] == "https://ebay.com/itm/123"
    assert offer["best_offer"] is True
    assert offer["regular_price"] is None


def test_bestbuy_happy_path(monkeypatch):
    box = {}

    def fake_get(url, **kw):
        box["url"] = url
        box["params"] = kw.get("params")
        return _Resp({"products": [BESTBUY_ITEM]})

    monkeypatch.setattr(shop, "_http_get", fake_get)
    result = shop.search_bestbuy("gadget x", 300.0)
    assert "error" not in result
    assert len(result["offers"]) == 1
    offer = result["offers"][0]
    assert offer["title"] == "Gadget X"
    assert offer["price"] == 199.99
    assert offer["regular_price"] == 249.99
    assert offer["shipping"] == 0.0
    assert offer["total"] == 199.99
    assert offer["seller"] == "Best Buy"
    assert offer["source"] == "bestbuy"
    assert offer["best_offer"] is False


def test_ebay_http_500_returns_error(monkeypatch):
    monkeypatch.setattr(
        shop, "_http_post",
        lambda url, **kw: _Resp({"access_token": "tok", "expires_in": 3600}),
    )
    monkeypatch.setattr(shop, "_http_get", lambda url, **kw: _Resp({}, status=500))
    result = shop.search_ebay("widget", 100.0)
    assert "error" in result


def test_bestbuy_http_500_returns_error(monkeypatch):
    monkeypatch.setattr(shop, "_http_get", lambda url, **kw: _Resp({}, status=500))
    result = shop.search_bestbuy("gadget", 100.0)
    assert "error" in result


def test_ebay_missing_key_error(monkeypatch):
    monkeypatch.delenv("EBAY_CLIENT_ID", raising=False)
    result = shop.search_ebay("widget", 100.0)
    assert "error" in result
    assert "EBAY_CLIENT_ID" in result["error"]


def test_bestbuy_missing_key_error(monkeypatch):
    monkeypatch.delenv("BESTBUY_API_KEY", raising=False)
    result = shop.search_bestbuy("gadget", 100.0)
    assert "error" in result
    assert "BESTBUY_API_KEY" in result["error"]


def test_ebay_drops_javascript_url(monkeypatch):
    bad_item = dict(EBAY_ITEM, itemWebUrl="javascript:alert(1)")
    monkeypatch.setattr(
        shop, "_http_post",
        lambda url, **kw: _Resp({"access_token": "tok", "expires_in": 3600}),
    )
    monkeypatch.setattr(shop, "_http_get", lambda url, **kw: _Resp({"itemSummaries": [bad_item]}))
    result = shop.search_ebay("widget", 100.0)
    assert result["offers"] == []
    assert result["skipped"] == 1


def test_bestbuy_drops_javascript_url(monkeypatch):
    bad_item = dict(BESTBUY_ITEM, url="javascript:alert(1)")
    monkeypatch.setattr(shop, "_http_get", lambda url, **kw: _Resp({"products": [bad_item]}))
    result = shop.search_bestbuy("gadget", 300.0)
    assert result["offers"] == []
    assert result["skipped"] == 1


def test_ebay_drops_over_budget_item(monkeypatch):
    expensive = dict(EBAY_ITEM, price={"value": "999.00", "currency": "USD"})
    monkeypatch.setattr(
        shop, "_http_post",
        lambda url, **kw: _Resp({"access_token": "tok", "expires_in": 3600}),
    )
    monkeypatch.setattr(shop, "_http_get", lambda url, **kw: _Resp({"itemSummaries": [expensive]}))
    result = shop.search_ebay("widget", 100.0)
    assert result["offers"] == []
    assert result["skipped"] == 1


def test_bestbuy_drops_over_budget_item(monkeypatch):
    expensive = dict(BESTBUY_ITEM, salePrice=999.00)
    monkeypatch.setattr(shop, "_http_get", lambda url, **kw: _Resp({"products": [expensive]}))
    result = shop.search_bestbuy("gadget", 300.0)
    assert result["offers"] == []
    assert result["skipped"] == 1


def test_ebay_token_reused_on_second_call(monkeypatch):
    post_calls = []

    def fake_post(url, **kw):
        post_calls.append(url)
        return _Resp({"access_token": "tok", "expires_in": 3600})

    monkeypatch.setattr(shop, "_http_post", fake_post)
    monkeypatch.setattr(shop, "_http_get", lambda url, **kw: _Resp({"itemSummaries": []}))

    shop.search_ebay("widget", 100.0)
    shop.search_ebay("widget", 100.0)
    assert len(post_calls) == 1


def test_bestbuy_query_words_sanitized(monkeypatch):
    box = {}

    def fake_get(url, **kw):
        box["url"] = url
        return _Resp({"products": []})

    monkeypatch.setattr(shop, "_http_get", fake_get)
    shop.search_bestbuy("tv); drop", 300.0)
    url = box["url"]
    assert ";" not in url
    assert " " not in url
    # No stray "(" beyond the one expression-opening paren the module itself
    # inserts around the search expression.
    assert url.count("(") == 1


def test_bestbuy_no_searchable_words(monkeypatch):
    result = shop.search_bestbuy(");(", 300.0)
    assert result == {"error": "query had no searchable words"}


def test_bestbuy_error_does_not_leak_api_key(monkeypatch):
    monkeypatch.setattr(shop, "_http_get", lambda url, **kw: _Resp({}, status=500))
    result = shop.search_bestbuy("gadget", 100.0)
    assert "bbkey123" not in result["error"]


def test_max_price_must_be_positive():
    assert shop.search_ebay("widget", -5.0) == {"error": "max_price must be a positive number"}
    assert shop.search_ebay("widget", 0) == {"error": "max_price must be a positive number"}
    assert shop.search_bestbuy("gadget", -5.0) == {"error": "max_price must be a positive number"}


def test_exactly_one_requests_get_and_post_in_module():
    """Every HTTP call must go through the module's own _http_get/_http_post
    seam so a test (or conftest's guard) has one place to stub — not
    scattered requests.get/post calls that would bypass it."""
    source = pathlib.Path(shop.__file__).read_text()
    assert source.count("requests.get(") == 1
    assert source.count("requests.post(") == 1
