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
