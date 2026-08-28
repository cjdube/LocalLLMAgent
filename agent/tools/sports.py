"""Final scores for the teams the user follows, from ESPN's public scoreboard API.

Source choice (deliberate, don't "fix" it): ESPN serves its own apps from
site.api.espn.com, and the scoreboard endpoint below is the same JSON shape for
every league — one parser covers MLB, NBA and NFL. No key, no account, three
requests a day. It is undocumented and ESPN publishes no terms for it, which
puts it outside the letter of the "official APIs only" rule in AGENTS.md; the
alternative (MLB's sanctioned statsapi.mlb.com) only covers baseball and would
mean maintaining a second parser for the other leagues. The tradeoff was made
knowingly.

Which day a game belongs to
---------------------------
ESPN buckets `dates=YYYYMMDD` by the **US Eastern** calendar but stamps each
event's `date` field in **UTC**. Asking for 2025-07-02 returns events stamped
2025-07-01, -02 and -03 — all correctly July 2 games in ET, plus a suspended
game ESPN files under its original July 1 start.

So: ask ESPN for the day and take what it returns. We deliberately do NOT
re-filter the events by their own UTC date. Doing so would drop the
suspended-game completion, and slicing an ISO stamp against a local calendar
day is the exact failure docs/timezones.md exists to prevent. Each game is
rendered with its start converted to local time, so one filed under an
unexpected day is visible rather than silently misplaced.

This assumes the user's local zone is US Eastern (it is — see the `location`
in config/preferences.json). Somewhere else, ESPN's day and the local day could
disagree at the edges; the local start time on each game is what would show it.

Doubleheaders (baseball only, in practice) come back as two separate events for
the same team on the same day. `game_number` / `games_that_day` are assigned
here in Python by start-time order rather than read off any ESPN flag: on the
real 2025-07-02 Red Sox pair, MLB's own API flagged both as `doubleHeader: "N"`
because one was a suspended-game completion.

Usage:
    python -m agent.tools.sports
    python -m agent.tools.sports --day 2025-07-02
    python -m agent.tools.sports --find-team "Red Sox" --league mlb
"""

import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from agent import prefs
from agent.dates import DATE_ARG_GUIDANCE, resolve_date, local_timezone
from agent.tools._http import http_error, print_result

# ESPN's path segment per league. Adding a league is adding a row here — the
# response shape is identical, so nothing else changes. (College basketball
# would be "basketball/mens-college-basketball", but it also needs
# groups=50&limit=400 or ESPN returns only ~17 featured games instead of ~155.)
LEAGUE_PATHS = {
    "mlb": "baseball/mlb",
    "nba": "basketball/nba",
    "nfl": "football/nfl",
}

_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# The scoreboard is a few hundred KB for a busy MLB day and runs inside the
# morning brief, so this bounds a stalled connection rather than a slow one.
TIMEOUT_S = 15

# The user's name, for the model-facing tool description below.
_NAME = prefs.user_name()

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fetch_scores",
        # Same reasoning as list_games (see agent/tools/games.py): a score is
        # something pretraining supplies a plausible answer for, so saying only
        # *when* to call the tool isn't enough — the description has to state
        # that the results are not something the model knows.
        "description": (
            f"Get the final scores of games played by the sports teams {_NAME} follows "
            "on a given day. Call this whenever he asks about a game, a score, or how a "
            "team did — including vague wording like 'how'd Boston do?' or 'did they "
            "win?'. Scores are NOT something you know: only the games this tool returns "
            "happened, and only the scores it returns are real. Never state a score, an "
            "opponent, or a date from your own knowledge, and never guess that a game "
            "took place — if the tool returns no games, say the team did not play that "
            "day. Report the date the tool gives back, not one you worked out yourself. "
            "In baseball a team can play twice in one day, so expect more than one game "
            "back and report both."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "day": {
                    "type": "string",
                    "description": "Which day to look up. " + DATE_ARG_GUIDANCE,
                },
            },
        },
    },
}


def _to_int(value) -> int | None:
    """ESPN sends scores as strings, and as "" or nothing for a game that never
    started. None means "no score", which the renderer shows as a status."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _summary_link(event: dict) -> str:
    """The Gamecast link for an event, or "" if ESPN didn't send one."""
    for link in event.get("links", []):
        if "summary" in link.get("rel", []) and link.get("href"):
            return link["href"]
    return ""


def _parse_event(event: dict, league: str, followed: dict, tz: ZoneInfo) -> dict | None:
    """Turn one ESPN event into a flat game dict, or None if it doesn't involve
    a followed team. `followed` maps ESPN team id -> the user's display label."""
    competition = (event.get("competitions") or [{}])[0]
    competitors = competition.get("competitors") or []

    # Match on team.id, not the competitor's own id: they happen to be equal in
    # these leagues, but team.id is the identity ESPN's teams directory hands
    # out, and that directory is what fills in config/preferences.json.
    def team_id(competitor: dict) -> str:
        return str((competitor.get("team") or {}).get("id", ""))

    mine = next((c for c in competitors if team_id(c) in followed), None)
    if mine is None:
        return None
    theirs = next((c for c in competitors if c is not mine), {})

    status = (event.get("status") or {}).get("type") or {}
    final = bool(status.get("completed"))
    my_score = _to_int(mine.get("score"))
    their_score = _to_int(theirs.get("score"))

    # The winner flag is ESPN's own call; scores only break the tie case, where
    # neither side is flagged a winner and both are still legitimate finals.
    if not final or my_score is None or their_score is None:
        result = None
    elif mine.get("winner"):
        result = "W"
    elif theirs.get("winner"):
        result = "L"
    else:
        result = "T" if my_score == their_score else ("W" if my_score > their_score else "L")

    start_local = ""
    try:
        start_local = datetime.fromisoformat(event.get("date", "")).astimezone(tz).isoformat()
    except ValueError:
        pass

    their_team = theirs.get("team") or {}
    return {
        "league": league,
        "team": followed[team_id(mine)],
        "opponent": their_team.get("shortDisplayName") or their_team.get("displayName") or "?",
        "home_away": mine.get("homeAway", ""),
        "team_score": my_score,
        "opponent_score": their_score,
        "result": result,
        "status": status.get("shortDetail") or status.get("description") or "",
        "final": final,
        "start_local": start_local,
        "url": _summary_link(event),
    }


def _number_games(games: list) -> None:
    """Stamp game_number / games_that_day per team, in start-time order.

    This is what makes a doubleheader legible: two Red Sox rows on one day are
    otherwise indistinguishable. Done in Python from position, never from a
    flag on the data — see the module docstring.
    """
    by_team: dict[tuple, list] = {}
    for game in games:
        by_team.setdefault((game["league"], game["team"]), []).append(game)
    for team_games in by_team.values():
        team_games.sort(key=lambda g: g["start_local"])
        for index, game in enumerate(team_games, start=1):
            game["game_number"] = index
            game["games_that_day"] = len(team_games)


def _teams_by_league(teams: list) -> dict:
    """Group configured teams into {league: {espn_id: label}}, skipping any
    league this module doesn't know — an unknown league is a config typo, and
    dropping it costs one team rather than the whole section."""
    grouped: dict[str, dict] = {}
    for team in teams:
        league = team.get("league")
        if league in LEAGUE_PATHS:
            grouped.setdefault(league, {})[str(team["id"])] = team.get("name") or str(team["id"])
    return grouped


def fetch_scores(day: str = "yesterday", teams: list | None = None) -> dict:
    """Callable entrypoint used by the agent loop's tool dispatcher.

    Returns {"date": ..., "games": [...]} plus an "errors" map naming any
    league whose fetch failed. One dead league never costs the others their
    games — but it is reported rather than swallowed, because "we couldn't tell"
    and "nobody played" must not look the same in the brief.
    """
    tz = ZoneInfo(local_timezone())
    date_str = resolve_date(day, today=datetime.now(tz).date(), prefer="past")

    teams = prefs.followed_teams() if teams is None else teams
    grouped = _teams_by_league(teams)
    result = {"date": date_str, "games": []}
    if not grouped:
        # No teams configured means the feature is off, not broken.
        return result

    compact = date_str.replace("-", "")
    games, errors = [], {}
    for league, followed in grouped.items():
        try:
            response = requests.get(
                f"{_BASE}/{LEAGUE_PATHS[league]}/scoreboard",
                params={"dates": compact},
                timeout=TIMEOUT_S,
            )
            response.raise_for_status()
            events = response.json().get("events", [])
        except Exception as e:
            errors[league] = http_error(e)["error"]
            continue
        for event in events:
            game = _parse_event(event, league, followed, tz)
            if game:
                games.append(game)

    _number_games(games)
    games.sort(key=lambda g: (g["start_local"], g["team"]))
    result["games"] = games
    if errors:
        result["errors"] = errors
    return result


def find_teams(query: str, league: str) -> dict:
    """Look up ESPN team ids by name, for filling in config/preferences.json.

    CLI-only — the model never sees this. Team ids belong in a config file the
    user edits once, not in anything the model has to transcribe.
    """
    if league not in LEAGUE_PATHS:
        return {"error": f"unknown league '{league}' (known: {', '.join(LEAGUE_PATHS)})"}
    try:
        response = requests.get(
            f"{_BASE}/{LEAGUE_PATHS[league]}/teams",
            params={"limit": 1000},
            timeout=TIMEOUT_S,
        )
        response.raise_for_status()
        entries = response.json()["sports"][0]["leagues"][0]["teams"]
    except Exception as e:
        return http_error(e)

    needle = query.strip().lower()
    matches = [
        {
            "league": league,
            "id": t["team"]["id"],
            "abbreviation": t["team"].get("abbreviation", ""),
            "name": t["team"].get("displayName", ""),
        }
        for t in entries
        if needle in t["team"].get("displayName", "").lower()
    ]
    return {"matches": matches}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", default="yesterday")
    parser.add_argument("--find-team", dest="find_team", default=None,
                        help="Look up ESPN team ids by name (needs --league).")
    parser.add_argument("--league", default=None, choices=sorted(LEAGUE_PATHS))
    args = parser.parse_args()

    if args.find_team:
        if not args.league:
            return print_result({"error": "--find-team also needs --league"})
        return print_result(find_teams(args.find_team, args.league))
    return print_result(fetch_scores(args.day))


if __name__ == "__main__":
    sys.exit(main())
