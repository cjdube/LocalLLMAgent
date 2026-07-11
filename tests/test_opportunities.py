"""Tests for the opportunities store/tools. _STORE_PATH is redirected to a tmp
file, so nothing touches the real config/opportunities.json."""

from datetime import datetime, timedelta

import pytest

from agent.tools import opportunities as opp


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setattr(opp, "_STORE_PATH", tmp_path / "opportunities.json")


def _candidate(item_id="edgar:0001-26-000001", signal="funded", **extra):
    return {
        "id": item_id, "source": item_id.split(":")[0], "signal": signal,
        "company": "Acme", "title": "Form D filed 2026-07-10",
        "url": "https://example.com", "location": "Boston, MA",
        "posted_at": "2026-07-10", **extra,
    }


# --------------------------------------------------------------------------- #
# insert / dedupe
# --------------------------------------------------------------------------- #

def test_insert_dedupes_by_natural_id():
    first = opp.insert_new_items([_candidate(), _candidate("hn:123", "hiring")])
    assert len(first) == 2
    # Same ids again (plus one genuinely new): only the new one inserts.
    second = opp.insert_new_items(
        [_candidate(), _candidate("hn:123", "hiring"), _candidate("hn:456", "hiring")]
    )
    assert [i["id"] for i in second] == ["hn:456"]
    assert all(i["status"] == "new" for i in first + second)


def test_insert_within_one_batch_dedupes_too():
    inserted = opp.insert_new_items([_candidate(), _candidate()])
    assert len(inserted) == 1


# --------------------------------------------------------------------------- #
# watchlist
# --------------------------------------------------------------------------- #

def test_watch_and_unwatch_company():
    out = opp.watch_company("Acme", "greenhouse", "acme")
    assert "id" in out
    assert [w["slug"] for w in opp.get_watchlist()] == ["acme"]
    # Unwatch matches by company name too, case-insensitively.
    assert opp.unwatch_company("acme")["removed"] is True
    assert opp.get_watchlist() == []


def test_watch_rejects_duplicates_and_bad_ats():
    opp.watch_company("Acme", "lever", "acme")
    assert "error" in opp.watch_company("Acme again", "lever", "acme")
    assert "error" in opp.watch_company("Bad", "workday", "bad")
    assert "error" in opp.watch_company("", "lever", "x")


def test_unwatch_unknown():
    assert "error" in opp.unwatch_company("nope")


# --------------------------------------------------------------------------- #
# status updates / listing
# --------------------------------------------------------------------------- #

def test_update_opportunity_statuses():
    item_id = opp.insert_new_items([_candidate()])[0]["id"]
    assert opp.update_opportunity(item_id, "interested")["status"] == "interested"
    assert "error" in opp.update_opportunity(item_id, "digested")  # not settable by chat
    assert "error" in opp.update_opportunity("missing", "dismissed")


def test_list_filters_and_caps():
    opp.insert_new_items(
        [_candidate(f"hn:{n}", "hiring") for n in range(opp._LIST_LIMIT + 5)]
        + [_candidate()]
    )
    listed = opp.list_opportunities()
    assert listed["count"] == opp._LIST_LIMIT + 6
    assert len(listed["opportunities"]) == opp._LIST_LIMIT  # context-bounded
    assert opp.list_opportunities(signal="funded")["count"] == 1
    assert opp.list_opportunities(status="dismissed")["count"] == 0


# --------------------------------------------------------------------------- #
# stalled flip
# --------------------------------------------------------------------------- #

def _days_ago_iso(days):
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")


def test_flip_stalled_by_posted_age_and_only_once():
    old = _candidate("greenhouse:acme:1", "hiring", posted_at=_days_ago_iso(60))
    fresh = _candidate("greenhouse:acme:2", "hiring", posted_at=_days_ago_iso(10))
    opp.insert_new_items([old, fresh])

    open_postings = {c["id"]: c["posted_at"] for c in (old, fresh)}
    flipped = opp.flip_stalled(open_postings, stalled_days=45)
    assert [i["id"] for i in flipped] == ["greenhouse:acme:1"]
    assert flipped[0]["signal"] == "stalled_search" and flipped[0]["status"] == "new"

    # Second sweep with the same board state: already flipped, nothing repeats.
    assert opp.flip_stalled(open_postings, stalled_days=45) == []


def test_flip_stalled_respects_dismissed_and_missing_dates():
    item = _candidate("lever:x:1", "hiring", posted_at=_days_ago_iso(90))
    item_id = opp.insert_new_items([item])[0]["id"]
    opp.update_opportunity(item_id, "dismissed")
    assert opp.flip_stalled({item_id: item["posted_at"]}, stalled_days=45) == []

    # No posted date falls back to first_seen (just now) — not stalled.
    no_date = opp.insert_new_items(
        [_candidate("ashby:y:1", "hiring", posted_at=None)]
    )[0]
    assert opp.flip_stalled({no_date["id"]: None}, stalled_days=45) == []


# --------------------------------------------------------------------------- #
# scores / digest lifecycle / prune
# --------------------------------------------------------------------------- #

def test_scores_digest_lifecycle():
    ids = [i["id"] for i in opp.insert_new_items(
        [_candidate(), _candidate("hn:1", "hiring")]
    )]
    opp.record_scores({ids[0]: (9, "Fresh raise — offer a velocity audit.")})
    pending = opp.pending_new_items()
    assert len(pending) == 2  # uncapped, unlike list_opportunities
    scored = next(i for i in pending if i["id"] == ids[0])
    assert scored["score"] == 9 and "velocity" in scored["angle"]

    opp.mark_digested(ids)
    assert opp.pending_new_items() == []
    # mark_digested only moves 'new' items — an 'interested' one stays.
    again = opp.insert_new_items([_candidate("hn:2", "hiring")])[0]["id"]
    opp.update_opportunity(again, "interested")
    opp.mark_digested([again])
    assert opp.list_opportunities(status="interested")["count"] == 1


def test_prune_drops_old_digested_keeps_interested():
    old = datetime.now() - timedelta(days=45)
    data = {
        "watchlist": [],
        "items": [
            {"id": "a", "status": "digested", "updated": old.isoformat()},
            {"id": "b", "status": "dismissed", "updated": old.isoformat()},
            {"id": "c", "status": "interested", "updated": old.isoformat()},
            {"id": "d", "status": "digested", "updated": datetime.now().isoformat()},
            {"id": "e", "status": "digested", "updated": "not-a-date"},  # kept, not guessed
        ],
    }
    opp._prune(data)
    assert [i["id"] for i in data["items"]] == ["c", "d", "e"]
