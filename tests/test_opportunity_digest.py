"""Tests for tasks/opportunity_digest.py — the leadership-title filter, the
defensive score parsing, and the run contracts: one dead source degrades
rather than killing the digest, nothing new sends nothing, a send failure
leaves items unreported (and the watermark unadvanced) so the next run
retries, and a stalled opening is reported exactly once. All collaborators
are monkeypatched; nothing touches the network, the model, or Gmail."""

from datetime import datetime, timedelta

import pytest

from agent.store import load_json
from agent.tools import opportunities as opp
from tasks import opportunity_digest as od


# --------------------------------------------------------------------------- #
# _is_leadership
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("title", [
    "VP of Engineering", "Vice President, Product", "Head of Product",
    "Director of Product Management", "Chief Technology Officer... CTO",
    "CTO", "Founding CPO", "Director, Engineering Technology",
])
def test_leadership_titles_match(title):
    assert od._is_leadership(title)


@pytest.mark.parametrize("title", [
    "Senior Software Engineer", "Product Manager", "Account Executive",
    "Head of Sales", "Director of Marketing", "Engineering Manager", "",
])
def test_non_leadership_titles_do_not_match(title):
    assert not od._is_leadership(title)


# --------------------------------------------------------------------------- #
# _parse_scores
# --------------------------------------------------------------------------- #

def test_parse_scores_defensively():
    valid = {"edgar:1", "hn:2"}
    text = (
        "Here are the scores:\n"            # prose: dropped
        "edgar:1|9|Fresh raise, offer a velocity audit\n"
        "hn:2|eleven|bad score\n"           # unparseable score: dropped
        "hn:999|5|unknown id\n"             # not in valid set: dropped
        "edgar:1|15|clamped\n"              # duplicate: last wins, clamped to 10
    )
    scores = od._parse_scores(text, valid)
    assert scores == {"edgar:1": (10, "clamped")}


def test_parse_scores_empty_and_none():
    assert od._parse_scores("", {"a"}) == {}
    assert od._parse_scores(None, {"a"}) == {}


# --------------------------------------------------------------------------- #
# build_and_send_digest / main contracts
# --------------------------------------------------------------------------- #

def _edgar_item(n=1):
    return {"id": f"edgar:{n}", "source": "edgar", "signal": "funded",
            "company": f"Startup {n}", "title": "Form D filed 2026-07-10",
            "url": "https://www.sec.gov/x", "location": "Portsmouth, NH",
            "posted_at": "2026-07-10"}


@pytest.fixture
def stubbed_run(tmp_path, monkeypatch):
    """Stub every collaborator for a happy-path run; tests then break the parts
    they're exercising. Returns the dict recording what got called."""
    monkeypatch.setattr(opp, "_STORE_PATH", tmp_path / "opportunities.json")
    monkeypatch.setattr(od, "STATE_PATH", tmp_path / "opportunities_state.json")

    seen = {"emails": [], "pushes": [], "failures": [], "prompts": []}
    monkeypatch.setattr(od, "poll_edgar",
                        lambda start, end, states: {"items": [_edgar_item()]})
    monkeypatch.setattr(od, "poll_ats", lambda watchlist: {"items": [], "errors": []})
    monkeypatch.setattr(od, "poll_hn", lambda: {"items": []})
    monkeypatch.setattr(od, "complete_text",
                        lambda **k: seen["prompts"].append(k) or "edgar:1|9|Offer a velocity audit.")
    monkeypatch.setattr(od, "send_email",
                        lambda subject, body, html=False:
                        seen["emails"].append((subject, body)) or {"message_id": "m1"})
    monkeypatch.setattr(od, "notify",
                        lambda **k: seen["pushes"].append(k) or {"ok": True})
    monkeypatch.setattr(od, "notify_failure",
                        lambda name, detail, logger=None: seen["failures"].append(str(detail)))
    return seen


def test_happy_path_scores_sends_and_pushes(stubbed_run):
    assert od.main() == 0
    assert len(stubbed_run["emails"]) == 1
    subject, body = stubbed_run["emails"][0]
    assert "Opportunity Digest" in subject
    assert "Startup 1" in body and "9/10" in body and "velocity audit" in body
    # Score 9 >= the default threshold of 8: an ntfy ping goes out too.
    assert len(stubbed_run["pushes"]) == 1
    assert stubbed_run["failures"] == []
    # Reported items leave 'new'; the EDGAR watermark advanced.
    assert opp.pending_new_items() == []
    assert load_json(od.STATE_PATH, {}).get("edgar_window_start")


def test_second_run_with_same_data_sends_nothing(stubbed_run):
    assert od.main() == 0
    assert od.main() == 0  # same poller output: all duplicates
    assert len(stubbed_run["emails"]) == 1
    assert len(stubbed_run["prompts"]) == 1  # nothing new was re-scored


def test_empty_day_sends_nothing_but_still_succeeds(stubbed_run, monkeypatch):
    monkeypatch.setattr(od, "poll_edgar", lambda *a: {"items": []})
    result = od.build_and_send_digest()
    assert result == {"sent": False, "new_items": 0}
    assert stubbed_run["emails"] == []
    assert load_json(od.STATE_PATH, {}).get("edgar_window_start")


def test_dead_source_degrades_and_holds_its_watermark(stubbed_run, monkeypatch):
    monkeypatch.setattr(od, "poll_edgar", lambda *a: {"error": "EDGAR 503"})
    monkeypatch.setattr(od, "poll_hn",
                        lambda: {"items": [{"id": "hn:7", "source": "hn",
                                            "signal": "hiring", "company": "TinyCo",
                                            "title": "Head of Product",
                                            "url": "https://news.ycombinator.com/item?id=7",
                                            "posted_at": None}]})
    monkeypatch.setattr(od, "complete_text", lambda **k: "hn:7|6|Say hi.")
    assert od.main() == 0  # HN item still went out
    assert "TinyCo" in stubbed_run["emails"][0][1]
    # EDGAR failed, so its window must NOT advance — those days get re-polled.
    assert load_json(od.STATE_PATH, {}) == {}


def test_send_failure_exits_nonzero_and_leaves_items_new(stubbed_run, monkeypatch):
    monkeypatch.setattr(od, "send_email",
                        lambda subject, body, html=False: {"error": "gmail 503"})
    assert od.main() == 1
    assert any("gmail 503" in f for f in stubbed_run["failures"])
    # Nothing was marked digested and the watermark held: next run re-reports.
    assert len(opp.pending_new_items()) == 1
    assert load_json(od.STATE_PATH, {}) == {}


def test_unparseable_scoring_output_still_sends(stubbed_run, monkeypatch):
    monkeypatch.setattr(od, "complete_text", lambda **k: "I cannot score these, sorry!")
    assert od.main() == 0
    assert len(stubbed_run["emails"]) == 1     # digest goes out unscored
    assert stubbed_run["pushes"] == []          # nothing crossed the threshold


def test_stalled_opening_reported_exactly_once(stubbed_run, monkeypatch):
    posted = (datetime.now() - timedelta(days=60)).isoformat(timespec="seconds")
    posting = {"id": "greenhouse:acme:1", "source": "ats", "signal": "hiring",
               "company": "Acme", "title": "VP of Engineering",
               "url": "https://boards.greenhouse.io/acme/1", "posted_at": posted}
    monkeypatch.setattr(od, "poll_edgar", lambda *a: {"items": []})
    monkeypatch.setattr(od, "poll_ats", lambda wl: {"items": [posting], "errors": []})
    monkeypatch.setattr(od, "complete_text", lambda **k: "greenhouse:acme:1|7|Offer to bridge.")

    assert od.main() == 0
    body = stubbed_run["emails"][0][1]
    # Already past the 45-day default when first seen: reported as stalled.
    assert "Stalled Searches" in body and "Acme" in body

    # The board still lists it on later runs — but it never re-alerts.
    assert od.main() == 0
    assert od.main() == 0
    assert len(stubbed_run["emails"]) == 1


def test_digest_html_escapes_and_guards_urls(stubbed_run, monkeypatch):
    hostile = {"id": "hn:x", "source": "hn", "signal": "hiring",
               "company": "<script>alert(1)</script>",
               "title": "Head of Product <b>now</b>",
               "url": "javascript:alert(1)", "posted_at": None}
    monkeypatch.setattr(od, "poll_edgar", lambda *a: {"items": []})
    monkeypatch.setattr(od, "poll_hn", lambda: {"items": [hostile]})
    monkeypatch.setattr(od, "complete_text", lambda **k: "hn:x|3|Skip.")
    assert od.main() == 0
    body = stubbed_run["emails"][0][1]
    assert "<script>" not in body
    assert "javascript:" not in body


def test_digest_footer_links_to_triage_page(stubbed_run, monkeypatch):
    monkeypatch.setenv("WREN_PUBLIC_URL", "https://mini.ts.net/")
    assert od.main() == 0
    assert 'https://mini.ts.net/opportunities' in stubbed_run["emails"][0][1]


def test_digest_footer_absent_without_public_url(stubbed_run, monkeypatch):
    monkeypatch.delenv("WREN_PUBLIC_URL", raising=False)
    assert od.main() == 0
    assert "/opportunities" not in stubbed_run["emails"][0][1]
