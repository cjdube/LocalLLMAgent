"""Tests for agent/tools/web_fetch.py — URL scheme validation, the Firecrawl
response parse, the truncation cap, and error mapping through the shared
_http.http_error. requests is monkeypatched — no network."""

import requests

from agent.tools import web_fetch


def _response(payload):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    return _Resp()


def _post_stub(payload, seen=None):
    def stub(url, json=None, headers=None, timeout=None):
        if seen is not None:
            seen.update({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return _response(payload)
    return stub


def _ok_payload(markdown="# Hello\n\nworld", title="Hello"):
    return {"success": True, "data": {"markdown": markdown, "metadata": {"title": title}}}


# --------------------------------------------------------------------------- #
# happy path + request shape
# --------------------------------------------------------------------------- #

def test_happy_path_returns_markdown_and_title(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-k")
    seen = {}
    monkeypatch.setattr(web_fetch.requests, "post", _post_stub(_ok_payload(), seen))
    out = web_fetch.fetch_webpage("https://example.com")
    assert out == {"url": "https://example.com", "title": "Hello", "markdown": "# Hello\n\nworld"}
    assert seen["url"] == web_fetch.SCRAPE_URL
    assert seen["json"] == {"url": "https://example.com", "formats": ["markdown"]}
    assert seen["headers"]["Authorization"] == "Bearer fc-k"
    assert seen["timeout"] is not None


def test_list_valued_title_takes_first(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-k")
    monkeypatch.setattr(web_fetch.requests, "post",
                        _post_stub(_ok_payload(title=["First", "Second"])))
    assert web_fetch.fetch_webpage("https://example.com")["title"] == "First"


def test_markdown_truncated_at_cap(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-k")
    monkeypatch.setattr(web_fetch, "MAX_CHARS", 10)
    monkeypatch.setattr(web_fetch.requests, "post", _post_stub(_ok_payload(markdown="x" * 50)))
    out = web_fetch.fetch_webpage("https://example.com")
    assert out["markdown"] == "x" * 10
    assert out["truncated"] is True


def test_max_chars_overrides_the_default_cap(monkeypatch):
    # evaluate_against feeds a one-shot call with the whole window to itself, so
    # MAX_CHARS (which defends the agent loop's tool-result budget) isn't its cap.
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-k")
    monkeypatch.setattr(web_fetch, "MAX_CHARS", 10)
    monkeypatch.setattr(web_fetch.requests, "post", _post_stub(_ok_payload(markdown="x" * 50)))

    out = web_fetch.fetch_webpage("https://example.com", max_chars=40)

    assert out["markdown"] == "x" * 40
    assert out["truncated"] is True


def test_max_chars_absent_or_junk_falls_back_to_the_default_cap(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-k")
    monkeypatch.setattr(web_fetch, "MAX_CHARS", 10)
    monkeypatch.setattr(web_fetch.requests, "post", _post_stub(_ok_payload(markdown="x" * 50)))

    for junk in (None, 0, -5):
        assert web_fetch.fetch_webpage("https://example.com", max_chars=junk)["markdown"] == "x" * 10


# --------------------------------------------------------------------------- #
# validation + degrade contracts
# --------------------------------------------------------------------------- #

def test_missing_key_short_circuits(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    out = web_fetch.fetch_webpage("https://example.com")
    assert "FIRECRAWL_API_KEY" in out["error"]


def test_non_http_schemes_rejected(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-k")
    monkeypatch.setattr(web_fetch.requests, "post", _post_stub(_ok_payload()))
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "ftp://x", "example.com", ""):
        assert "error" in web_fetch.fetch_webpage(bad), bad


def test_http_error_maps_status(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-k")
    resp = requests.Response()
    resp.status_code = 402

    def raise_http(*a, **k):
        raise requests.exceptions.HTTPError(response=resp)

    monkeypatch.setattr(web_fetch.requests, "post", raise_http)
    out = web_fetch.fetch_webpage("https://example.com")
    assert out["error"].startswith("HTTP 402")


def test_firecrawl_level_failure_is_an_error(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-k")
    monkeypatch.setattr(web_fetch.requests, "post",
                        _post_stub({"success": False, "error": "blocked"}))
    out = web_fetch.fetch_webpage("https://example.com")
    assert "blocked" in out["error"]


def test_empty_markdown_is_an_error(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-k")
    monkeypatch.setattr(web_fetch.requests, "post", _post_stub(_ok_payload(markdown="  ")))
    assert "error" in web_fetch.fetch_webpage("https://example.com")


def test_the_truncated_flag_comes_before_the_markdown_it_describes(monkeypatch):
    """It used to be appended last, back when MAX_CHARS was the same 8000 as the
    loop's own cap, so the wrapper put every truncated fetch ~520 chars over and
    the loop cut the tail — deleting the one key that said the content was cut.
    16 times in the logs."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-k")
    monkeypatch.setattr(web_fetch, "MAX_CHARS", 10)
    monkeypatch.setattr(web_fetch.requests, "post", _post_stub(_ok_payload(markdown="x" * 50)))

    keys = list(web_fetch.fetch_webpage("https://example.com"))
    assert keys.index("truncated") < keys.index("markdown")


def test_a_full_page_plus_its_wrapper_fits_the_tools_own_cap():
    """The loop gives fetch_webpage room for MAX_CHARS plus the wrapper around
    it, so the tool's deliberate cut is never re-cut by the blind one.

    MAX_CHARS is read from the environment (WEB_FETCH_MAX_CHARS), so this also
    catches a config raise that forgets the matching raise in loop.py — the two
    numbers only work as a pair."""
    import json

    from agent.loop import TOOL_RESULT_CHAR_CAPS
    biggest = {"url": "https://example.com/" + "u" * 200, "title": "T" * 300,
               "truncated": True, "markdown": "x" * web_fetch.MAX_CHARS}
    assert len(json.dumps(biggest)) < TOOL_RESULT_CHAR_CAPS["fetch_webpage"]
