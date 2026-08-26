"""Tests for agent/activity_log.py — the prior-day range math, the source
compaction and its exclusions, and the persist-or-email fallback that ScribeJay's
journal entries and tasks/daily_synthesis.py share. Collaborators are
monkeypatched; nothing touches the model, the vault, or Gmail.

The journaling-only helpers that used to live here are in tests/test_journal.py."""

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from agent import activity_log as lc

_TZ = ZoneInfo("America/New_York")
_LOG = logging.getLogger("test_activity_log")


# --------------------------------------------------------------------------- #
# prior_day
# --------------------------------------------------------------------------- #

def test_prior_day_returns_yesterday_full_span():
    # Run Monday 2026-07-13 05:00 (the launchd shape): covers all of Sunday 07-12.
    start, end, day = lc.prior_day(now=datetime(2026, 7, 13, 5, 0, tzinfo=_TZ))
    assert day == date(2026, 7, 12)
    assert (start.year, start.month, start.day) == (2026, 7, 12)
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert (end.hour, end.minute, end.second) == (23, 59, 59)


def test_prior_day_crosses_month_boundary():
    start, end, day = lc.prior_day(now=datetime(2026, 8, 1, 5, 0, tzinfo=_TZ))
    assert day == date(2026, 7, 31)


# --------------------------------------------------------------------------- #
# compact_sites — the learnings-only domain exclusions
# --------------------------------------------------------------------------- #

@pytest.fixture
def excluded(monkeypatch):
    """_EXCLUDED_DOMAINS is built from preferences at import, so patch the list
    itself rather than the JSON (same shape as _COMMUNITY_KEYWORDS)."""
    monkeypatch.setattr(lc, "_EXCLUDED_DOMAINS", ["sharepoint.com", "signupgenius.com"])


def _site(domain, visits=1):
    return {"domain": domain, "title": f"{domain} page", "url": f"https://{domain}/x", "visits": visits}


def test_compact_sites_drops_excluded_domain(excluded):
    out = lc.compact_sites([_site("www.signupgenius.com"), _site("ai.google.dev")])
    assert [s["domain"] for s in out] == ["ai.google.dev"]


def test_compact_sites_drops_subdomains_of_excluded(excluded):
    # The whole point of suffix matching: one entry covers every M365 tenant.
    out = lc.compact_sites([_site("acme.sharepoint.com"), _site("ai.google.dev")])
    assert [s["domain"] for s in out] == ["ai.google.dev"]


def test_compact_sites_keeps_lookalike_domain(excluded):
    # Must not match by bare substring: "notsharepoint.com" isn't a subdomain.
    out = lc.compact_sites([_site("notsharepoint.com")])
    assert [s["domain"] for s in out] == ["notsharepoint.com"]


def test_compact_sites_excludes_before_the_cap(excluded, monkeypatch):
    # An excluded site must not consume one of the MAX_CHROME_SITES slots.
    monkeypatch.setattr(lc, "MAX_CHROME_SITES", 2)
    out = lc.compact_sites([
        _site("acme.sharepoint.com", visits=99),
        _site("ai.google.dev", visits=5),
        _site("tailscale.com", visits=3),
    ])
    assert [s["domain"] for s in out] == ["ai.google.dev", "tailscale.com"]


def test_compact_sites_strips_port_before_matching(monkeypatch):
    monkeypatch.setattr(lc, "_EXCLUDED_DOMAINS", ["127.0.0.1"])
    out = lc.compact_sites([_site("127.0.0.1:8420"), _site("ai.google.dev")])
    assert [s["domain"] for s in out] == ["ai.google.dev"]


def test_compact_sites_drops_url_and_sorts_by_visits(excluded):
    out = lc.compact_sites([_site("a.com", visits=1), _site("b.com", visits=9)])
    assert [s["domain"] for s in out] == ["b.com", "a.com"]
    assert "url" not in out[0]


def test_compact_sites_carries_page_paths_through(excluded):
    site = _site("ai.google.dev")
    site["pages"] = [{"path": "/docs/models", "visits": 9}, {"path": "/docs/pricing", "visits": 4}]
    out = lc.compact_sites([site])
    assert out[0]["pages"] == ["/docs/models", "/docs/pricing"]


def test_compact_sites_caps_pages_per_site(excluded, monkeypatch):
    monkeypatch.setattr(lc, "MAX_PAGES_PER_SITE", 2)
    site = _site("ai.google.dev")
    site["pages"] = [{"path": f"/p{i}", "visits": i} for i in range(6)]
    assert len(lc.compact_sites([site])[0]["pages"]) == 2


def test_compact_sites_omits_pages_when_absent(excluded):
    # fetch_chrome_history's default returns no `pages` — degrade to domain+title.
    assert "pages" not in lc.compact_sites([_site("ai.google.dev")])[0]


# --------------------------------------------------------------------------- #
# is_excluded_text — subject-matter exclusions a domain can't express
# --------------------------------------------------------------------------- #

@pytest.fixture
def excluded_kw(monkeypatch):
    monkeypatch.setattr(lc, "_EXCLUDED_KEYWORDS", ["acme"])


def test_is_excluded_text_is_case_insensitive(excluded_kw):
    assert lc.is_excluded_text("Acme Speakers Bureau")
    assert lc.is_excluded_text("volunteering for acme")
    assert not lc.is_excluded_text("Gemini API pricing")
    assert not lc.is_excluded_text("")


def test_compact_sites_drops_site_whose_title_matches(excluded_kw):
    # The domain is fine; the subject isn't.
    site = _site("www.eventbrite.com")
    site["title"] = "Acme Speakers Bureau — Register"
    out = lc.compact_sites([site, _site("ai.google.dev")])
    assert [s["domain"] for s in out] == ["ai.google.dev"]


def test_compact_sites_drops_only_the_matching_path_not_the_site(excluded_kw):
    # One excluded page on an otherwise reviewable site must not drop the site.
    site = _site("www.linkedin.com")
    site["pages"] = [{"path": "/feed/", "visits": 5},
                     {"path": "/company/acme/", "visits": 2}]
    out = lc.compact_sites([site])
    assert out[0]["pages"] == ["/feed/"]


def test_compact_sites_no_keywords_keeps_everything(monkeypatch):
    monkeypatch.setattr(lc, "_EXCLUDED_KEYWORDS", [])
    site = _site("www.eventbrite.com")
    site["title"] = "Acme Speakers Bureau"
    assert len(lc.compact_sites([site])) == 1


def test_compact_sites_no_exclusions_configured_keeps_everything(monkeypatch):
    monkeypatch.setattr(lc, "_EXCLUDED_DOMAINS", [])
    out = lc.compact_sites([_site("www.signupgenius.com")])
    assert [s["domain"] for s in out] == ["www.signupgenius.com"]


# --------------------------------------------------------------------------- #
# persist_or_email — the write-fails -> email fallback contract
# --------------------------------------------------------------------------- #

@pytest.fixture
def spy(monkeypatch):
    seen = {"emails": [], "failures": [], "writes": []}
    monkeypatch.setattr(lc, "write_entry",
                        lambda content, prefix, day, directory=None: seen["writes"].append((prefix, content, directory)) or {"written": True})
    monkeypatch.setattr(lc, "send_email",
                        lambda subject, body: seen["emails"].append((subject, body)) or {"message_id": "m1"})
    monkeypatch.setattr(lc, "notify_failure",
                        lambda name, detail, logger=None: seen["failures"].append(str(detail)))
    return seen


def test_persist_writes_and_sends_no_email(spy):
    lc.persist_or_email("body", "Daily-Chrome", date(2026, 7, 12), "subj", "daily_chrome_learnings", _LOG)
    assert spy["writes"] and not spy["emails"] and spy["failures"] == []


def test_persist_write_failure_emails_the_draft(spy, monkeypatch):
    monkeypatch.setattr(lc, "write_entry", lambda content, prefix, day, directory=None: {"error": "target dir not found"})
    lc.persist_or_email("body", "Daily-Chrome", date(2026, 7, 12), "subj", "daily_chrome_learnings", _LOG)
    assert spy["emails"] == [("subj", "body")]
    assert any("vault write failed" in f for f in spy["failures"])


def test_persist_write_and_email_both_failing_raises(spy, monkeypatch):
    monkeypatch.setattr(lc, "write_entry", lambda content, prefix, day, directory=None: {"error": "target dir not found"})
    monkeypatch.setattr(lc, "send_email", lambda subject, body: {"error": "gmail 503"})
    with pytest.raises(RuntimeError, match="both failed"):
        lc.persist_or_email("body", "Daily-Chrome", date(2026, 7, 12), "subj", "daily_chrome_learnings", _LOG)
