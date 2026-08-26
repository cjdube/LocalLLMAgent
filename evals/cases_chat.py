"""Chat tool-calling cases for the model bake-off.

Each case is one user message put through agent.loop.advance() with the real
chat system prompt and the real (lazily-loaded) tool set. Tool results are
CANNED — the harness never dispatches a real tool — so a case is reproducible
and no eval run can touch the calendar, send mail, or hit an API.

The set is weighted toward the failures recorded in docs/model-constraints.md,
because those are the ones that cost weeks of silent breakage:

  * `calendar_weekday`  — the model doing weekday arithmetic itself
  * `games_vague`       — a catalogue answered from pretraining
  * `chain_calendar_task` — describing a gated write instead of calling it

The rest are the common asks, plus three that need no tool at all (a model that
reaches for one anyway is as broken as one that won't).

Every date in a fixture is computed RELATIVE TO TODAY, never written out. The
chat system prompt bakes in the real current date, so an absolute fixture date
stops meaning what the case was written to mean and the scoring quietly
inverts: `calendar_upcoming` pinned an event to 2026-08-17, and three days
later the model that correctly answered "that was yesterday, nothing is coming
up" scored zero while the model that called it "tomorrow" scored full marks.
`tests/test_run_eval.py` fails if an absolute date reappears here.
"""

from datetime import date, datetime, timedelta

from agent.dates import resolve_date

# Returned for any tool the case doesn't name explicitly. Shaped like a real
# tool result (a dict) so the model sees what it normally would.
DEFAULT_TOOL_RESULT = {"ok": True, "items": []}


def _has(sub):
    """Predicate: the argument value contains `sub`, case-insensitively."""
    return lambda v: sub.lower() in str(v or "").lower()


def _nonempty(v):
    return bool(str(v or "").strip())


# --------------------------------------------------------------------------- #
# Relative fixture dates
#
# Resolved once at import. A run that crosses midnight would carry the starting
# day's fixtures, which is harmless at the ~2h per model this takes — but it is
# the reason not to start one at 23:00.
# --------------------------------------------------------------------------- #

def _day(offset: int) -> date:
    """The local date `offset` days from today. Negative is the past."""
    return datetime.now().date() + timedelta(days=offset)


def _at(offset: int, clock: str) -> str:
    """A fixture timestamp, e.g. _at(-1, "08:00:00") -> '2026-08-17T08:00:00'."""
    return f"{_day(offset).isoformat()}T{clock}"


def _long(d: date) -> str:
    """'Tuesday, August 18, 2026' — the shape the calendar tool returns."""
    return d.strftime("%A, %B %-d, %Y")


def _day_markers(d: date) -> list[str]:
    """The two ways a reply names a day: 'wednesday' and 'august 19'."""
    return [d.strftime("%A").lower(), d.strftime("%B %-d").lower()]


def _wrong_days(correct: date, *wrong: date) -> list[str]:
    """Markers a correct reply must never contain, given the right day.

    Drops any marker that is a substring of the correct day's own markers —
    the scorer matches substrings, so forbidding 'august 2' would fail a reply
    that correctly said 'August 20'."""
    right = _day_markers(correct)
    return [m for w in wrong for m in _day_markers(w)
            if not any(m in r for r in right)]


# The day the tool would actually resolve "next tuesday" to, via production's
# own resolver — so the fixture can never disagree with what Wren would return.
_NEXT_TUESDAY = date.fromisoformat(resolve_date("next tuesday"))
# The event `calendar_upcoming` puts on the calendar. Two days out, so a reply
# that says "today" or "tomorrow" is wrong in a way the markers can catch.
_UPCOMING = _day(2)

CASES = [
    # ---- the recorded failures ------------------------------------------- #
    {
        "id": "calendar_weekday",
        "prompt": "What's on my calendar next Tuesday?",
        "expect_tool": "get_events_by_date",
        # The whole point of the fix: the PHRASE goes to the tool, the tool
        # resolves it. A model that sends a resolved date has re-introduced the
        # bug even when the date happens to be right.
        "arg_checks": {"start": _has("tuesday"), "end": _has("tuesday")},
        "tool_results": {
            "get_events_by_date": {
                "range": _long(_NEXT_TUESDAY),
                "resolved_start": _NEXT_TUESDAY.isoformat(),
                "resolved_end": _NEXT_TUESDAY.isoformat(),
                "events": [{"summary": "Dentist",
                            "start": f"{_NEXT_TUESDAY.isoformat()}T14:00:00"}],
            },
        },
        "final_must_contain": ["dentist"],
        # It must quote the date the TOOL returned, not one it worked out —
        # the original bug answered the Tuesday question with the Wednesday.
        "final_must_not_contain": _wrong_days(_NEXT_TUESDAY,
                                              _NEXT_TUESDAY + timedelta(days=1)),
    },
    {
        "id": "games_vague",
        # "let's play a game" is a request to ACT, not a question about what
        # exists — the phrasing that exposed the fabrication in 2 of 12 replays.
        "prompt": "I'm bored. Let's play a game.",
        "expect_tool": "list_games",
        "tool_results": {
            "list_games": {"games": [
                {"name": "Weigh Anchor", "url": "/games/weigh-anchor",
                 "description": "A single-player sailing puzzle.", "status": "up"},
            ]},
        },
        "final_must_contain": ["weigh anchor"],
        "final_must_not_contain": ["wordle", "sudoku", "chess", "hangman", "trivia"],
    },
    {
        "id": "chain_calendar_task",
        # The describe-instead-of-perform bug. Two steps: read the calendar,
        # then a confirmation-gated task write. Scored on whether the write was
        # actually CALLED — a turn that narrates it and stops is the failure.
        #
        # This used to read Strava first. Journaling moved to ScribeJay, so Wren has
        # no fetch_strava; the chain was re-anchored on tools she still owns. The
        # bug being guarded is about the SECOND step, so the first step's source
        # never mattered.
        "prompt": "Look at what's on my calendar tomorrow and add a task to prep for the first meeting.",
        # Deliberately NOT expect_any_of with the read: reading and stopping is
        # the exact failure this case exists to catch, so only the write counts
        # as having done the job.
        "expect_tool": "create_task",
        "arg_checks": {"title": _nonempty},
        "tool_results": {
            "get_events_by_date": {"events": [{
                "summary": "Quarterly roadmap review", "id": "evt1",
                "start": _at(1, "09:00:00"), "end": _at(1, "10:00:00"),
            }]},
            "get_upcoming_events": {"events": [{
                "summary": "Quarterly roadmap review", "id": "evt1",
                "start": _at(1, "09:00:00"), "end": _at(1, "10:00:00"),
            }]},
        },
    },
    # ---- catalogue tools -------------------------------------------------- #
    {
        "id": "games_direct",
        "prompt": "What games can we play?",
        "expect_tool": "list_games",
        "tool_results": {
            "list_games": {"games": [
                {"name": "Weigh Anchor", "url": "/games/weigh-anchor",
                 "description": "A single-player sailing puzzle.", "status": "up"},
            ]},
        },
        "final_must_contain": ["weigh anchor"],
        "final_must_not_contain": ["wordle", "sudoku", "chess"],
    },
    {
        "id": "sports_vague",
        # A score is the other shape where pretraining hands over a plausible
        # answer, and "how'd Boston do" names no tool, no league and no day —
        # the wording a keyword pre-loader would miss and a model would fill in.
        "prompt": "How'd Boston do last night?",
        "expect_tool": "fetch_scores",
        "tool_results": {
            "fetch_scores": {"date": _day(-1).isoformat(), "games": [
                {"league": "mlb", "team": "Red Sox", "opponent": "Diamondbacks",
                 "home_away": "home", "team_score": 9, "opponent_score": 4,
                 "result": "W", "status": "Final", "final": True,
                 "start_local": _at(-1, "19:10:00"),
                 "game_number": 1, "games_that_day": 1},
            ]},
        },
        "final_must_contain": ["diamondbacks"],
        # Invented opponents from a plausible-sounding AL East schedule. The
        # failure is a reply shaped exactly like a real one, so the guard has to
        # be on the names rather than on the shape.
        "final_must_not_contain": ["yankees", "orioles", "blue jays", "rays"],
    },
    {
        "id": "sports_doubleheader",
        # Baseball's one structural oddity: two finals on one day. A model that
        # reports only the first is wrong in a way nothing downstream catches.
        "prompt": "Did the Red Sox win yesterday?",
        "expect_tool": "fetch_scores",
        "tool_results": {
            "fetch_scores": {"date": _day(-1).isoformat(), "games": [
                {"league": "mlb", "team": "Red Sox", "opponent": "Reds",
                 "home_away": "home", "team_score": 5, "opponent_score": 3,
                 "result": "W", "status": "Final", "final": True,
                 "start_local": _at(-1, "13:35:00"),
                 "game_number": 1, "games_that_day": 2},
                {"league": "mlb", "team": "Red Sox", "opponent": "Reds",
                 "home_away": "home", "team_score": 4, "opponent_score": 8,
                 "result": "L", "status": "Final", "final": True,
                 "start_local": _at(-1, "19:10:00"),
                 "game_number": 2, "games_that_day": 2},
            ]},
        },
        # Both results have to appear, or the second game went unreported.
        "final_must_contain": ["reds", "5", "3", "4", "8"],
    },
    {
        "id": "projects_list",
        "prompt": "What projects have I been working on lately?",
        "expect_tool": "list_projects",
        "tool_results": {
            "list_projects": {"projects": [
                {"name": "LocalLLMAgent", "summary": "Wren, a local-first agent.",
                 "last_commit": _day(-1).isoformat()},
                {"name": "ObsidianWikiAgent", "summary": "Turns notes into wiki pages.",
                 "last_commit": _day(-5).isoformat()},
            ]},
        },
        "final_must_contain": ["localllmagent"],
    },
    {
        "id": "nudges_recent",
        "prompt": "Have you noticed anything worth telling me about this week?",
        "expect_tool": "list_nudges",
        "tool_results": {
            "list_nudges": {"nudges": [
                {"date": _day(-2).isoformat(), "text": "You read about MLX quantization "
                 "and your Ollama note covers the same ground."},
            ]},
        },
        "final_must_contain": ["mlx"],
    },
    {
        "id": "notifications_sent",
        "prompt": "Did you push anything to my phone yesterday?",
        "expect_tool": "list_notifications",
        "tool_results": {
            # The real shape: one finished `summary` block, no raw rows. The
            # stamp is already written out for a human, so the model reads a
            # weekday and time rather than doing date math on an ISO string.
            "list_notifications": {
                "summary": "1 notification(s) from the last 7 days, newest first:\n"
                           f"- {_day(-1).strftime('%a %b %-d')}, 8:00 AM"
                           " — **Morning brief** — Sent.",
                "total": 1, "shown": 1, "days": 7,
            },
        },
        "final_must_contain": ["morning brief"],
        # The push IS yesterday's, so the answer is yes. A denial means the
        # model mis-placed the stamp against today's date.
        "final_must_not_contain": ["didn't", "nothing"],
    },
    # ---- everyday reads ---------------------------------------------------- #
    {
        "id": "weather_today",
        "prompt": "What's the weather doing today?",
        "expect_tool": "fetch_weather",
        "tool_results": {
            "fetch_weather": {"today": {"high_f": 78, "low_f": 61,
                                        "summary": "Partly cloudy"}},
        },
        "final_must_contain": ["78"],
    },
    {
        "id": "calendar_tomorrow",
        "prompt": "Anything on my calendar tomorrow?",
        "expect_tool": "get_events_by_date",
        "arg_checks": {"start": _has("tomorrow")},
        "tool_results": {
            "get_events_by_date": {
                "range": _long(_day(1)), "resolved_start": _day(1).isoformat(),
                "resolved_end": _day(1).isoformat(), "events": [],
            },
        },
    },
    {
        "id": "calendar_upcoming",
        "prompt": "What's coming up on my calendar?",
        "expect_any_of": ["get_upcoming_events", "get_events_by_date"],
        "tool_results": {
            "get_upcoming_events": {"events": [
                {"summary": "Team sync", "start": _at(2, "10:00:00")},
            ]},
            "get_events_by_date": {"range": "next week", "events": [
                {"summary": "Team sync", "start": _at(2, "10:00:00")},
            ]},
        },
        "final_must_contain": ["team sync"],
        # Naming the event is not enough — it has to land on the right day.
        # gemma4:12b-mlx called this same event "today" and "tomorrow" in 2 of
        # 3 runs while scoring 3/3 on the name alone (2026-08-18).
        "final_must_not_contain": ["yesterday"] + _wrong_days(_UPCOMING, _day(0), _day(1)),
    },
    {
        "id": "tasks_due_soon",
        "prompt": "What have I got due soon?",
        "expect_any_of": ["get_tasks_due_soon", "get_tasks"],
        "tool_results": {
            "get_tasks_due_soon": {"tasks": [
                {"title": "Renew car registration", "due": _day(1).isoformat()},
            ]},
            "get_tasks": {"tasks": [
                {"title": "Renew car registration", "due": _day(1).isoformat()},
            ]},
        },
        "final_must_contain": ["registration"],
    },
    {
        "id": "run_from_the_record",
        # The seam the ScribeJay split created, stated as an eval: ScribeJay logs each
        # Strava activity onto the calendar, so Wren answers "how far did I run"
        # by READING THE RECORD. She has no fetch_strava any more — a run that
        # reaches for one is a regression in the split, not a model miss.
        "prompt": "How far did I run last week?",
        "expect_any_of": ["get_events_by_date", "get_upcoming_events"],
        "tool_results": {
            "get_events_by_date": {"events": [
                {"summary": "Morning Run", "id": "evt9",
                 "description": "Distance: 6.8 km\nDuration: 41 min",
                 "start": _at(-4, "07:10:00"), "end": _at(-4, "07:51:00")},
            ]},
        },
        "final_must_contain": ["6.8"],
    },
    {
        "id": "web_search",
        "prompt": "Search the web for what's new with Apple's M5 chip.",
        "expect_tool": "search_web",
        "arg_checks": {"query": _nonempty},
        "tool_results": {
            "search_web": {"results": [
                {"title": "Apple announces M5", "url": "https://example.com/m5",
                 "snippet": "Apple's M5 adds a wider neural engine."},
            ]},
        },
        "final_must_contain": ["m5"],
    },
    # ---- gated writes ------------------------------------------------------ #
    # advance() returns {"type": "confirm"} for these and executes nothing, so
    # they are safe to run. What's scored is that the call was MADE.
    {
        "id": "task_create",
        "prompt": "Add a task to renew the car registration, due Friday.",
        "expect_tool": "create_task",
        "arg_checks": {"title": _nonempty},
    },
    {
        "id": "reminder_set",
        "prompt": "Remind me to call the dentist at 3pm tomorrow.",
        "expect_tool": "set_reminder",
        # Same rule as dates: the phrase goes through verbatim, Python resolves it.
        "arg_checks": {"message": _nonempty, "when": _has("tomorrow")},
    },
    {
        "id": "memory_remember",
        "prompt": "Remember that I prefer morning meetings over afternoon ones.",
        "expect_any_of": ["remember", "pin"],
        "arg_checks": {"text": _nonempty},
    },
    # ---- what happens AFTER the confirmation ------------------------------- #
    # Every case above stops at the first confirmation card, which is where the
    # harness used to stop too — so the continuation turn was never measured.
    # On 2026-08-18 that turn re-issued the same calendar write and drew a
    # second card; declining it drew a third. These two cases answer the pause
    # (through the same stubbed dispatch) and score the turn that follows.
    {
        "id": "confirm_then_stop",
        "prompt": "Put yardwork on my calendar for 10am tomorrow, for an hour.",
        "expect_tool": "log_calendar_event",
        "confirm": "approve",
        "arg_checks": {"summary": _nonempty, "start": _nonempty},
        "tool_results": {
            "log_calendar_event": {
                "created": True, "summary": "yardwork",
                # The shape log_calendar_event now echoes back, so the reply has
                # a time to quote instead of restating its own guess.
                "when": f"{_long(_day(1))}, 10:00 AM to 11:00 AM",
                "event_id": "evt-1", "html_link": "https://cal/evt-1",
            },
        },
        # The reply must report the write, not promise it again.
        "final_must_contain": ["10"],
    },
    {
        "id": "decline_then_stop",
        # A decline used to be handed back as {"error": ...} — the same shape a
        # crashed tool returns — so the model retried and drew a fresh card.
        "prompt": "Put yardwork on my calendar for 10am tomorrow, for an hour.",
        "expect_tool": "log_calendar_event",
        "confirm": "decline",
        # Claims of success only. Kept narrow on purpose: a bare "added it"
        # also matches the CORRECT reply "I haven't added it".
        "final_must_not_contain": ["i've added", "i have added", "i've scheduled",
                                   "it's on your calendar"],
    },
    # ---- no tool should be called ------------------------------------------ #
    {
        "id": "no_tool_greeting",
        "prompt": "Hey Wren, how's your day going?",
        "expect_tool": None,
    },
    {
        "id": "no_tool_knowledge",
        "prompt": "What's the capital of Portugal?",
        "expect_tool": None,
        "final_must_contain": ["lisbon"],
    },
    {
        "id": "no_tool_ambiguous",
        # Deliberately meaningless. The right move is to ask what "that" is,
        # not to guess a tool.
        "prompt": "Can you sort that out for me?",
        "expect_tool": None,
    },
]
