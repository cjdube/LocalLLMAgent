"""Tests for agent/tools/sports.py.

Every fixture below is trimmed from a real ESPN scoreboard response — the
doubleheader is the genuine Red Sox pair from 2025-07-02, including the
suspended-game completion ESPN stamps with the previous day's UTC date. No
network: requests.get is monkeypatched in every test that reaches it.
"""

import pytest
import requests

from agent.tools import sports

RED_SOX = [{"league": "mlb", "id": "2", "name": "Red Sox"}]
PATRIOTS = [{"league": "nfl", "id": "17", "name": "Patriots"}]

# America/New_York, so an evening game is the case where the UTC date and the
# local date disagree — pinned per CLAUDE.md rather than inherited from the host.
EASTERN = "America/New_York"


def _competitor(team_id, name, score, home_away, winner):
    return {
        "id": team_id,
        "homeAway": home_away,
        "winner": winner,
        "team": {"id": team_id, "shortDisplayName": name, "displayName": f"The {name}"},
        "score": score,
    }


def _event(event_id, date, competitors, completed=True, short_detail="Final"):
    return {
        "id": event_id,
        "date": date,
        "status": {"type": {"completed": completed, "shortDetail": short_detail}},
        "links": [{
            "rel": ["summary", "desktop", "event"],
            "href": f"https://www.espn.com/mlb/game/_/gameId/{event_id}",
        }],
        "competitions": [{"id": event_id, "competitors": competitors}],
    }


# The real 2025-07-02 Red Sox doubleheader. Note the first event's UTC date is
# 2025-07-01 — ESPN files a suspended game under its original start.
DOUBLEHEADER = [
    _event("401696179", "2025-07-01T23:10Z", [
        _competitor("2", "Red Sox", "5", "home", True),
        _competitor("17", "Reds", "3", "away", False),
    ]),
    _event("401696194", "2025-07-02T23:10Z", [
        _competitor("2", "Red Sox", "4", "home", False),
        _competitor("17", "Reds", "8", "away", True),
    ]),
]

# A game involving nobody the user follows, to prove the team filter works.
OTHER_GAME = _event("401696200", "2025-07-02T22:00Z", [
    _competitor("19", "Yankees", "1", "home", False),
    _competitor("21", "Mets", "6", "away", True),
])


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _pin_timezone(monkeypatch):
    monkeypatch.setenv("TIMEZONE", EASTERN)


def _stub_get(monkeypatch, by_league):
    """Serve a fixture per league. `by_league` maps a league key to either an
    events list or an exception to raise."""
    calls = []

    def fake_get(url, params=None, timeout=None):
        assert timeout, "every HTTP call needs an explicit timeout"
        calls.append((url, params))
        league = next(k for k, v in sports.LEAGUE_PATHS.items() if f"/{v}/" in url)
        events = by_league.get(league, [])
        if isinstance(events, Exception):
            raise events
        return _Response({"events": events})

    monkeypatch.setattr(sports.requests, "get", fake_get)
    return calls


# ---- doubleheaders ----------------------------------------------------------

def test_doubleheader_returns_both_games_numbered(monkeypatch):
    _stub_get(monkeypatch, {"mlb": DOUBLEHEADER})
    games = sports.fetch_scores(day="2025-07-02", teams=RED_SOX)["games"]

    assert len(games) == 2
    assert [g["game_number"] for g in games] == [1, 2]
    assert all(g["games_that_day"] == 2 for g in games)
    assert [g["result"] for g in games] == ["W", "L"]
    assert [(g["team_score"], g["opponent_score"]) for g in games] == [(5, 3), (4, 8)]


def test_single_game_is_not_numbered_as_a_doubleheader(monkeypatch):
    _stub_get(monkeypatch, {"mlb": [DOUBLEHEADER[1]]})
    games = sports.fetch_scores(day="2025-07-02", teams=RED_SOX)["games"]
    assert len(games) == 1
    assert games[0]["games_that_day"] == 1


# ---- day handling -----------------------------------------------------------

def test_event_stamped_a_day_earlier_in_utc_is_kept(monkeypatch):
    """ESPN buckets by ET and stamps in UTC, and files a suspended game under
    its original start. Re-filtering by the event's own UTC date would silently
    drop that game — the exact failure docs/timezones.md warns about."""
    _stub_get(monkeypatch, {"mlb": DOUBLEHEADER})
    games = sports.fetch_scores(day="2025-07-02", teams=RED_SOX)["games"]
    assert len(games) == 2, "the 2025-07-01-stamped game was dropped"


def test_evening_game_keeps_its_local_day(monkeypatch):
    """23:10Z on July 1 is 7:10 PM Eastern on July 1 — the evening case where a
    naive UTC read would report the wrong calendar day."""
    _stub_get(monkeypatch, {"mlb": [DOUBLEHEADER[0]]})
    game = sports.fetch_scores(day="2025-07-02", teams=RED_SOX)["games"][0]
    assert game["start_local"].startswith("2025-07-01T19:10")


def test_relative_day_is_resolved_and_reported(monkeypatch):
    calls = _stub_get(monkeypatch, {"mlb": []})
    result = sports.fetch_scores(day="2025-07-02", teams=RED_SOX)
    # The tool reports the day it actually used, so a reply can quote it.
    assert result["date"] == "2025-07-02"
    assert calls[0][1] == {"dates": "20250702"}


# ---- filtering and shape ----------------------------------------------------

def test_games_without_a_followed_team_are_excluded(monkeypatch):
    _stub_get(monkeypatch, {"mlb": DOUBLEHEADER + [OTHER_GAME]})
    games = sports.fetch_scores(day="2025-07-02", teams=RED_SOX)["games"]
    assert {g["team"] for g in games} == {"Red Sox"}


def test_configured_name_is_the_label_and_opponent_comes_from_espn(monkeypatch):
    _stub_get(monkeypatch, {"mlb": [DOUBLEHEADER[0]]})
    teams = [{"league": "mlb", "id": "2", "name": "Boston"}]
    game = sports.fetch_scores(day="2025-07-02", teams=teams)["games"][0]
    assert game["team"] == "Boston"
    assert game["opponent"] == "Reds"
    assert game["home_away"] == "home"
    assert game["url"] == "https://www.espn.com/mlb/game/_/gameId/401696179"


def test_one_request_per_league_not_per_team(monkeypatch):
    calls = _stub_get(monkeypatch, {"mlb": [], "nfl": []})
    teams = [
        {"league": "mlb", "id": "2", "name": "Red Sox"},
        {"league": "mlb", "id": "19", "name": "Yankees"},
        {"league": "nfl", "id": "17", "name": "Patriots"},
    ]
    sports.fetch_scores(day="2025-07-02", teams=teams)
    assert len(calls) == 2


def test_unknown_league_is_skipped_not_fatal(monkeypatch):
    _stub_get(monkeypatch, {"mlb": [DOUBLEHEADER[0]]})
    teams = RED_SOX + [{"league": "quidditch", "id": "1", "name": "Gryffindor"}]
    assert len(sports.fetch_scores(day="2025-07-02", teams=teams)["games"]) == 1


def test_no_configured_teams_is_silent_not_an_error():
    # Nothing configured means the feature is off. No HTTP call is made, so no
    # stub is needed — a request here would fail the test by hitting the network.
    result = sports.fetch_scores(day="2025-07-02", teams=[])
    assert result == {"date": "2025-07-02", "games": []}


# ---- non-final games --------------------------------------------------------

def test_postponed_game_has_no_score_or_result(monkeypatch):
    postponed = _event("401696300", "2025-07-02T23:10Z", [
        _competitor("2", "Red Sox", None, "home", False),
        _competitor("17", "Reds", None, "away", False),
    ], completed=False, short_detail="Postponed")
    _stub_get(monkeypatch, {"mlb": [postponed]})
    game = sports.fetch_scores(day="2025-07-02", teams=RED_SOX)["games"][0]
    assert game["final"] is False
    assert game["result"] is None
    assert game["team_score"] is None
    assert game["status"] == "Postponed"


def test_tie_is_reported_as_a_tie(monkeypatch):
    tied = _event("401696400", "2025-11-02T18:00Z", [
        _competitor("17", "Patriots", "17", "home", False),
        _competitor("20", "Jets", "17", "away", False),
    ])
    _stub_get(monkeypatch, {"nfl": [tied]})
    assert sports.fetch_scores(day="2025-11-02", teams=PATRIOTS)["games"][0]["result"] == "T"


# ---- degradation ------------------------------------------------------------

def test_one_dead_league_does_not_cost_the_others(monkeypatch):
    _stub_get(monkeypatch, {
        "mlb": DOUBLEHEADER,
        "nfl": requests.exceptions.RequestException("boom"),
    })
    teams = RED_SOX + PATRIOTS
    result = sports.fetch_scores(day="2025-07-02", teams=teams)
    assert len(result["games"]) == 2
    assert "nfl" in result["errors"] and "boom" in result["errors"]["nfl"]


def test_errors_key_is_absent_when_everything_worked(monkeypatch):
    _stub_get(monkeypatch, {"mlb": DOUBLEHEADER})
    assert "errors" not in sports.fetch_scores(day="2025-07-02", teams=RED_SOX)


def test_unparseable_event_date_does_not_crash_the_run(monkeypatch):
    broken = _event("401696500", "not-a-date", [
        _competitor("2", "Red Sox", "1", "home", True),
        _competitor("17", "Reds", "0", "away", False),
    ])
    _stub_get(monkeypatch, {"mlb": [broken]})
    game = sports.fetch_scores(day="2025-07-02", teams=RED_SOX)["games"][0]
    assert game["start_local"] == ""
    assert game["result"] == "W"


# ---- tool description -------------------------------------------------------

def test_description_denies_pretraining_as_a_source():
    """A score is the shape where pretraining supplies a plausible answer, so
    the description has to say the results are not something the model knows —
    the same rule that fixed list_games. See docs/model-constraints.md."""
    description = sports.TOOL_SCHEMA["function"]["description"].lower()
    assert "not something you know" in description
    assert "never" in description
    # It must also say what to do with an empty result, or the model fills it in.
    assert "did not play" in description
    # And that one team can have two games in a day, or it reports only the first.
    assert "more than one game" in description
