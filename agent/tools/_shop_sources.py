"""Read-only price-lookup adapters for the shopping pipeline.

search_ebay and search_bestbuy each take a query and a price ceiling and
return normalized offers — same shape regardless of source, so a caller
(agent/tools/shopping.py) can merge and rank them without knowing
which API answered. Private (`_`-prefixed): these are collaborators for that
pipeline, not a model-facing tool in agent/toolset.py, same as
_opportunities_feed.py is a collaborator for opportunities.py.

Keys: EBAY_CLIENT_ID + EBAY_CLIENT_SECRET, and BESTBUY_API_KEY, resolved
from config/.env or the environment via agent.tools._http.resolve_key.

Usage:
    python -m agent.tools._shop_sources --source ebay --query "usb-c hub" --max 40
"""

import argparse
import sys
import time

import requests

from agent.tools._http import http_error, load_env, missing_key_error, print_result, resolve_key
from tasks._urls import safe_url

load_env()

EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
BESTBUY_PRODUCTS_URL = "https://api.bestbuy.com/v1/products"

TIMEOUT = 15


def _http_get(url, **kwargs):
    """The module's one call to requests.get — every GET goes through here so
    a test (or tests/conftest.py's suite-wide network guard) has a single
    seam to monkeypatch instead of the shared requests.get object."""
    return requests.get(url, timeout=TIMEOUT, **kwargs)


def _http_post(url, **kwargs):
    """The module's one call to requests.post — see _http_get."""
    return requests.post(url, timeout=TIMEOUT, **kwargs)


# Cache for eBay's client-credentials token, keyed by nothing (one token
# scope in use) so repeated calls in one process reuse it rather than fetching
# a fresh token per search. Module-level, in-memory only — no file store,
# since the token is only ever needed within a single run.
_ebay_token_cache: dict = {}

# Margin subtracted from a token's reported expires_in so a token that is
# about to expire is refreshed a little early rather than used right up to
# the wire and rejected mid-search.
_TOKEN_EXPIRY_MARGIN = 60


def _title_case_trim(title: str) -> str:
    """Trim a title to 200 chars, defensively (a missing/odd title becomes "")."""
    return str(title or "")[:200]


def _get_ebay_token(client_id: str, client_secret: str) -> str:
    """Fetch (or reuse) an eBay OAuth client-credentials token."""
    now = time.time()
    cached = _ebay_token_cache.get("token")
    if cached and _ebay_token_cache.get("expires_at", 0) > now:
        return cached

    resp = _http_post(
        EBAY_TOKEN_URL,
        auth=(client_id, client_secret),
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
    )
    resp.raise_for_status()
    payload = resp.json()
    token = payload["access_token"]
    expires_in = payload.get("expires_in", 0)
    _ebay_token_cache["token"] = token
    _ebay_token_cache["expires_at"] = now + max(0, expires_in - _TOKEN_EXPIRY_MARGIN)
    return token


def _normalize_ebay_item(item: dict, max_price: float) -> dict | None:
    """Map one eBay itemSummaries[] entry to the normalized offer shape, or
    None if it's malformed or over budget."""
    try:
        price = float(item["price"]["value"])
    except (KeyError, TypeError, ValueError):
        return None
    if price > max_price:
        return None

    url = safe_url(item.get("itemWebUrl", ""))
    if not url:
        return None

    shipping = None
    shipping_options = item.get("shippingOptions") or []
    if shipping_options:
        try:
            shipping = float(shipping_options[0]["shippingCost"]["value"])
        except (KeyError, TypeError, ValueError):
            shipping = None

    total = price + shipping if shipping is not None else None
    buying_options = item.get("buyingOptions") or []

    return {
        "title": _title_case_trim(item.get("title")),
        "price": price,
        "shipping": shipping,
        "total": total,
        "seller": str((item.get("seller") or {}).get("username", "")),
        "source": "ebay",
        "condition": str(item.get("condition", "")),
        "url": url,
        "best_offer": "BEST_OFFER" in buying_options,
        "regular_price": None,
    }


def search_ebay(query: str, max_price: float, limit: int = 10) -> dict:
    """Search eBay's Browse API for fixed-price/best-offer listings at or under
    max_price. Returns {"offers": [...]} or {"error": ...}; never raises."""
    if not isinstance(max_price, (int, float)) or isinstance(max_price, bool) or max_price <= 0:
        return {"error": "max_price must be a positive number"}
    if not query or not query.strip():
        return {"error": "query must not be empty"}

    client_id = resolve_key("EBAY_CLIENT_ID")
    if not client_id:
        return missing_key_error("EBAY_CLIENT_ID")
    client_secret = resolve_key("EBAY_CLIENT_SECRET")
    if not client_secret:
        return missing_key_error("EBAY_CLIENT_SECRET")

    limit = max(1, min(int(limit or 10), 50))

    try:
        token = _get_ebay_token(client_id, client_secret)
        resp = _http_get(
            EBAY_SEARCH_URL,
            params={
                "q": query.strip(),
                "limit": limit,
                "filter": (
                    f"price:[..{max_price}],priceCurrency:USD,"
                    "buyingOptions:{FIXED_PRICE|BEST_OFFER}"
                ),
            },
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            },
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        return http_error(e)

    try:
        incoming = raw.get("itemSummaries") or []
        offers = []
        for item in incoming:
            offer = _normalize_ebay_item(item, max_price)
            if offer is not None:
                offers.append(offer)
        result = {"offers": offers}
        skipped = len(incoming) - len(offers)
        if skipped:
            result["skipped"] = skipped
        return result
    except Exception as e:
        return {"error": f"parse error: {e}"}


def _sanitize_bestbuy_words(query: str) -> list:
    """Reduce a query to alphanumeric-only words, dropping anything empty.

    The words go straight into the request URL path (Best Buy's parenthesized
    search-expression syntax), so no character but [A-Za-z0-9] may survive —
    this is what keeps a query like "tv); drop" from injecting expression
    syntax rather than just searching oddly.
    """
    words = []
    for word in (query or "").split():
        cleaned = "".join(c for c in word if c.isalnum())
        if cleaned:
            words.append(cleaned)
    return words


def _normalize_bestbuy_item(item: dict, max_price: float) -> dict | None:
    """Map one Best Buy products[] entry to the normalized offer shape, or
    None if it's malformed or over budget."""
    try:
        price = float(item["salePrice"])
    except (KeyError, TypeError, ValueError):
        return None
    if price > max_price:
        return None

    url = safe_url(item.get("url", ""))
    if not url:
        return None

    regular_price = None
    try:
        regular_price = float(item["regularPrice"])
    except (KeyError, TypeError, ValueError):
        regular_price = None

    if item.get("freeShipping"):
        shipping = 0.0
    else:
        try:
            shipping = float(item["shippingCost"])
        except (KeyError, TypeError, ValueError):
            shipping = None

    total = price + shipping if shipping is not None else None

    return {
        "title": _title_case_trim(item.get("name")),
        "price": price,
        "shipping": shipping,
        "total": total,
        "seller": "Best Buy",
        "source": "bestbuy",
        "condition": str(item.get("condition") or "new"),
        "url": url,
        "best_offer": False,
        "regular_price": regular_price,
    }


def search_bestbuy(query: str, max_price: float, limit: int = 10) -> dict:
    """Search Best Buy's Products API for items at or under max_price.
    Returns {"offers": [...]} or {"error": ...}; never raises."""
    if not isinstance(max_price, (int, float)) or isinstance(max_price, bool) or max_price <= 0:
        return {"error": "max_price must be a positive number"}

    api_key = resolve_key("BESTBUY_API_KEY")
    if not api_key:
        return missing_key_error("BESTBUY_API_KEY")

    words = _sanitize_bestbuy_words(query)
    if not words:
        return {"error": "query had no searchable words"}

    limit = max(1, min(int(limit or 10), 25))
    expr = "&".join(f"search={word}" for word in words) + f"&salePrice<={max_price}"
    url = f"{BESTBUY_PRODUCTS_URL}({expr})"

    try:
        resp = _http_get(
            url,
            params={
                "apiKey": api_key,
                "format": "json",
                "pageSize": limit,
                "show": "name,salePrice,regularPrice,url,shippingCost,freeShipping,condition",
            },
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        # http_error redacts every query parameter's value, including apiKey,
        # so the key itself never reaches a log line or a model-facing result.
        return http_error(e)

    try:
        incoming = raw.get("products") or []
        offers = []
        for item in incoming:
            offer = _normalize_bestbuy_item(item, max_price)
            if offer is not None:
                offers.append(offer)
        result = {"offers": offers}
        skipped = len(incoming) - len(offers)
        if skipped:
            result["skipped"] = skipped
        return result
    except Exception as e:
        return {"error": f"parse error: {e}"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, choices=["ebay", "bestbuy"])
    parser.add_argument("--query", required=True)
    parser.add_argument("--max", dest="max_price", required=True, type=float)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    if args.source == "ebay":
        result = search_ebay(args.query, args.max_price, args.limit)
    else:
        result = search_bestbuy(args.query, args.max_price, args.limit)
    return print_result(result)


if __name__ == "__main__":
    sys.exit(main())
