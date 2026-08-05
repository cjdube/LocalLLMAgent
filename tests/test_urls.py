"""Tests for tasks._urls — the scheme allow-list guarding every externally
sourced URL before it reaches HTML or Markdown.

These moved here from tests/test_morning_brief.py when the three identical
copies of this function were consolidated. Keeping them attached to one
caller's test file was part of the original problem: the other two copies had
no direct coverage at all.
"""

from tasks._urls import safe_url


def test_safe_url_allows_http():
    assert safe_url("http://example.com/x") == "http://example.com/x"


def test_safe_url_allows_https():
    assert safe_url("https://example.com/x") == "https://example.com/x"


def test_safe_url_rejects_javascript_scheme():
    assert safe_url("javascript:alert(1)") == ""


def test_safe_url_rejects_data_scheme():
    assert safe_url("data:text/html,<script>alert(1)</script>") == ""


def test_safe_url_rejects_other_schemes():
    assert safe_url("ftp://example.com/x") == ""


def test_safe_url_rejects_relative_and_empty():
    assert safe_url("/relative/path") == ""
    assert safe_url("") == ""


def test_safe_url_tolerates_non_string():
    # urlparse raises AttributeError on None; a feed with a null url should
    # cost its link, not the whole digest.
    assert safe_url(None) == ""
