"""Tests for tasks/_learnings_common.py — the prior-day range math, the URL
guard and the deterministic video list, and the persist-or-email fallback that
both daily learnings tasks share. Collaborators are monkeypatched; nothing
touches the model, the vault, or Gmail."""

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from tasks import _learnings_common as lc

_TZ = ZoneInfo("America/New_York")
_LOG = logging.getLogger("test_learnings_common")


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
    out = lc.compact_sites([_site("aarpsharex.sharepoint.com"), _site("ai.google.dev")])
    assert [s["domain"] for s in out] == ["ai.google.dev"]


def test_compact_sites_keeps_lookalike_domain(excluded):
    # Must not match by bare substring: "notsharepoint.com" isn't a subdomain.
    out = lc.compact_sites([_site("notsharepoint.com")])
    assert [s["domain"] for s in out] == ["notsharepoint.com"]


def test_compact_sites_excludes_before_the_cap(excluded, monkeypatch):
    # An excluded site must not consume one of the MAX_CHROME_SITES slots.
    monkeypatch.setattr(lc, "MAX_CHROME_SITES", 2)
    out = lc.compact_sites([
        _site("aarpsharex.sharepoint.com", visits=99),
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
    monkeypatch.setattr(lc, "_EXCLUDED_KEYWORDS", ["aarp"])


def test_is_excluded_text_is_case_insensitive(excluded_kw):
    assert lc.is_excluded_text("AARP Speakers Bureau")
    assert lc.is_excluded_text("volunteering for aarp")
    assert not lc.is_excluded_text("Gemini API pricing")
    assert not lc.is_excluded_text("")


def test_compact_sites_drops_site_whose_title_matches(excluded_kw):
    # The domain is fine; the subject isn't.
    site = _site("www.eventbrite.com")
    site["title"] = "AARP NH Speakers Bureau — Register"
    out = lc.compact_sites([site, _site("ai.google.dev")])
    assert [s["domain"] for s in out] == ["ai.google.dev"]


def test_compact_sites_drops_only_the_matching_path_not_the_site(excluded_kw):
    # One excluded page on an otherwise reviewable site must not drop the site.
    site = _site("www.linkedin.com")
    site["pages"] = [{"path": "/feed/", "visits": 5},
                     {"path": "/company/aarp/", "visits": 2}]
    out = lc.compact_sites([site])
    assert out[0]["pages"] == ["/feed/"]


def test_compact_sites_no_keywords_keeps_everything(monkeypatch):
    monkeypatch.setattr(lc, "_EXCLUDED_KEYWORDS", [])
    site = _site("www.eventbrite.com")
    site["title"] = "AARP NH Speakers Bureau"
    assert len(lc.compact_sites([site])) == 1


def test_compact_sites_no_exclusions_configured_keeps_everything(monkeypatch):
    monkeypatch.setattr(lc, "_EXCLUDED_DOMAINS", [])
    out = lc.compact_sites([_site("www.signupgenius.com")])
    assert [s["domain"] for s in out] == ["www.signupgenius.com"]


# --------------------------------------------------------------------------- #
# safe_url / videos_section
# --------------------------------------------------------------------------- #

def test_videos_section_renders_linked_list():
    section = lc.videos_section([
        {"title": "Git Deep Dive", "channel": "LearnThatStack",
         "url": "https://www.youtube.com/watch?v=abc"},
        {"title": "No Channel Vid", "channel": "", "url": "https://youtu.be/xyz"},
    ])
    lines = section.splitlines()
    assert lines[0] == "### Videos Liked"
    assert lines[1] == "- [Git Deep Dive](https://www.youtube.com/watch?v=abc) — LearnThatStack"
    assert lines[2] == "- [No Channel Vid](https://youtu.be/xyz)"


def test_videos_section_empty_states_none():
    assert "None" in lc.videos_section([])


def test_videos_section_drops_bad_scheme_url_but_keeps_title():
    section = lc.videos_section([
        {"title": "Sketchy", "channel": "X", "url": "javascript:alert(1)"},
    ])
    assert "- Sketchy — X" in section
    assert "javascript:" not in section


# --------------------------------------------------------------------------- #
# has_substantive_content
# --------------------------------------------------------------------------- #

def test_has_substantive_content_true_with_a_real_bullet():
    assert lc.has_substantive_content("### X\n- **GitHub:** reviewed a PR")


def test_has_substantive_content_false_when_only_none_markers():
    text = ("## Daily Log\n\n### What I Worked On\n- **None:** [No qualifying items]\n\n"
            "### Tools & Tech Encountered\n- **None:** [No qualifying items]")
    assert not lc.has_substantive_content(text)


def test_has_substantive_content_false_when_no_bullets():
    assert not lc.has_substantive_content("## Daily Log: July 12, 2026\n\nheader only")


# --------------------------------------------------------------------------- #
# persist_or_email — the write-fails -> email fallback contract
# --------------------------------------------------------------------------- #

@pytest.fixture
def spy(monkeypatch):
    seen = {"emails": [], "failures": [], "writes": []}
    monkeypatch.setattr(lc, "write_entry",
                        lambda content, prefix, day: seen["writes"].append((prefix, content)) or {"written": True})
    monkeypatch.setattr(lc, "send_email",
                        lambda subject, body: seen["emails"].append((subject, body)) or {"message_id": "m1"})
    monkeypatch.setattr(lc, "notify_failure",
                        lambda name, detail, logger=None: seen["failures"].append(str(detail)))
    return seen


def test_persist_writes_and_sends_no_email(spy):
    lc.persist_or_email("body", "Daily-Chrome", date(2026, 7, 12), "subj", "daily_chrome_learnings", _LOG)
    assert spy["writes"] and not spy["emails"] and spy["failures"] == []


def test_persist_write_failure_emails_the_draft(spy, monkeypatch):
    monkeypatch.setattr(lc, "write_entry", lambda content, prefix, day: {"error": "drive not mounted"})
    lc.persist_or_email("body", "Daily-Chrome", date(2026, 7, 12), "subj", "daily_chrome_learnings", _LOG)
    assert spy["emails"] == [("subj", "body")]
    assert any("vault write failed" in f for f in spy["failures"])


def test_persist_write_and_email_both_failing_raises(spy, monkeypatch):
    monkeypatch.setattr(lc, "write_entry", lambda content, prefix, day: {"error": "drive not mounted"})
    monkeypatch.setattr(lc, "send_email", lambda subject, body: {"error": "gmail 503"})
    with pytest.raises(RuntimeError, match="both failed"):
        lc.persist_or_email("body", "Daily-Chrome", date(2026, 7, 12), "subj", "daily_chrome_learnings", _LOG)
