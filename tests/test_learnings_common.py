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
