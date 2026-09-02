"""Tests for agent/tools/_http.py — the shared credential resolution, error
mapping, and CLI-print helpers every HTTP-backed tool module reuses."""

import json

import requests

from agent.tools import _http


def test_resolve_key_prefers_arg_over_env(monkeypatch):
    monkeypatch.setenv("SOME_KEY", "from-env")
    assert _http.resolve_key("SOME_KEY", "from-arg") == "from-arg"


def test_resolve_key_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("SOME_KEY", "from-env")
    assert _http.resolve_key("SOME_KEY") == "from-env"
    assert _http.resolve_key("SOME_KEY", None) == "from-env"


def test_resolve_key_none_when_unset(monkeypatch):
    monkeypatch.delenv("SOME_KEY", raising=False)
    assert _http.resolve_key("SOME_KEY") is None


def test_missing_key_error_names_the_key():
    err = _http.missing_key_error("TAVILY_API_KEY")
    assert "TAVILY_API_KEY" in err["error"]


# Credential redaction. These cases build the exceptions the way requests
# itself does — with a real message carrying a real URL — because the tests
# below them passed for months against message-less stubs while the live path
# was emailing the OpenWeatherMap key into the morning brief.

_URL = ("https://api.openweathermap.org/data/2.5/forecast"
        "?q=Bedford&appid=SENTINELKEY&units=imperial&cnt=24")


def test_raise_for_status_does_not_leak_the_key_in_the_query_string():
    # The real shape: requests builds "401 Client Error: ... for url: <url>"
    # from response.url, which carries the whole query string.
    resp = requests.Response()
    resp.status_code = 401
    resp.reason = "Unauthorized"
    resp.url = _URL
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        err = _http.http_error(exc)["error"]
    assert "SENTINELKEY" not in err
    assert err.startswith("HTTP 401")
    # The parameter names survive, so the message still says which call failed.
    assert "appid=<redacted>" in err


def test_connection_error_does_not_leak_the_key_either():
    # The leak is not specific to raise_for_status: a ConnectionError embeds
    # "Max retries exceeded with url: <path?query>" as a bare path, no scheme.
    exc = requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='api.openweathermap.org', port=443): "
        "Max retries exceeded with url: /data/2.5/forecast"
        "?q=Bedford&appid=SENTINELKEY (Caused by NewConnectionError())")
    err = _http.http_error(exc)["error"]
    assert "SENTINELKEY" not in err
    assert err.startswith("network error")


def test_the_catch_all_branch_redacts_too():
    # An exception type nobody anticipated is the one that would carry a URL
    # out, so the phase branch is redacted as well.
    err = _http.http_error(ValueError(f"boom while fetching {_URL}"), phase="parse")["error"]
    assert "SENTINELKEY" not in err
    assert err.startswith("parse error")


def test_redaction_keeps_text_without_a_query_string_intact():
    assert _http.redact_query_values("Read timed out. (read timeout=10)") == (
        "Read timed out. (read timeout=10)")


def test_http_error_maps_status_when_response_present():
    resp = requests.Response()
    resp.status_code = 503
    exc = requests.exceptions.HTTPError(response=resp)
    assert _http.http_error(exc)["error"].startswith("HTTP 503")


def test_http_error_handles_httperror_without_response():
    # A raised HTTPError may carry no response object; the mapper must not crash.
    assert _http.http_error(requests.exceptions.HTTPError())["error"].startswith("HTTP ?")


def test_http_error_maps_generic_request_exception():
    exc = requests.exceptions.ConnectionError("refused")
    assert _http.http_error(exc)["error"].startswith("network error")


def test_http_error_falls_back_to_phase_for_other_exceptions():
    assert _http.http_error(ValueError("boom"), phase="parse")["error"].startswith("parse error")


def test_print_result_prints_and_returns_zero_on_success(capsys):
    assert _http.print_result({"ok": True}) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_print_result_returns_one_on_error(capsys):
    assert _http.print_result({"error": "nope"}) == 1
    capsys.readouterr()  # drain
