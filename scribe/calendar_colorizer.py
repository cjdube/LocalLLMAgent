"""Recolor yesterday's Google Calendar events by category. Non-interactive —
run by launchd every day at 5pm, covering the day that just ended.

Usage:
    python -m tasks.calendar_colorizer
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import prefs
from agent.loop import complete_text, resolve_backend, warm_model
from agent.tools.calendar import (
    CATEGORY_COLORS,
    SESSION_BLOCK_SOURCE_PREFIX,
    _local_timezone,
    get_events_in_range,
    set_event_color,
)
from agent.tools.email import send_email
from tasks._common import notify_failure, setup_logger

VALID_COLOR_IDS = {color_id for color_id, _ in CATEGORY_COLORS.values()}

# The colorId the model should use only when it can't tell what an event is.
_FALLBACK_COLOR = prefs.category_color_by_role("fallback", "11")


def _classification_table() -> str:
    rows = ["| Category | Color name | colorId |", "|----------|-----------|---------|"]
    for c in prefs.calendar_categories():
        label = c["name"].replace("/", " / ")
        if c.get("hint"):
            label = f"{label} {c['hint']}"
        rows.append(f"| {label} | {c.get('color_name', '')} | {c['color_id']} |")
    return "\n".join(rows)


CLASSIFY_SYSTEM_PROMPT = f"""You are {prefs.user_name()}'s calendar color-coding assistant. You are given a \
JSON list of yesterday's calendar events, each with an "n" (its number) and a "summary" (title). For \
each event, decide which category it belongs to and return the matching Google Calendar \
colorId, using EXACTLY this mapping:

{_classification_table()}

Best-guess every event from its title. Only use colorId "{_FALLBACK_COLOR}" when you genuinely cannot tell \
what category an event belongs to — do not use it as a default.

Output ONLY a single JSON object mapping each event's "n" to its chosen colorId string, \
nothing else — no preamble, no explanation, no markdown code fences. Example:
{{"1": "1", "2": "6"}}
"""


def _classify_input(events: list) -> list:
    """Number the events 1..N for the model instead of showing it Google's
    26-character event ids.

    The ids used to go out and come back as the response's keys, which cost a
    run: on 2026-07-25 the model spent all 3072 num_predict tokens in its
    thinking block transcribing an id character by character and second-guessing
    itself, finished with done_reason 'length', and emitted no content at all —
    an empty response the parser could only report as bad JSON. Even successful
    runs mis-copied that id (dropping two characters), so the event silently
    matched nothing and never got colored. Small integers are cheap to copy and
    trivial to verify, and Python owns the number -> id mapping."""
    return [{"n": n, "summary": e["summary"]} for n, e in enumerate(events, 1)]


def _yesterday_range() -> tuple[datetime, datetime]:
    tz = ZoneInfo(_local_timezone())
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    return yesterday, today - timedelta(microseconds=1)


def _parse_classification(raw_response: str) -> dict:
    """Parse the model's {event_number: colorId} response, rejecting anything
    that isn't a JSON object — the model is told to emit exactly that shape, but
    a small local model can drift (prose preamble, a list, code fences)."""
    if not raw_response.strip():
        raise RuntimeError(
            "Model returned an empty response — a thinking model that spends its "
            "whole num_predict budget reasoning emits no content at all "
            "(done_reason 'length'). Check OLLAMA_NUM_PREDICT and the prompt size."
        )
    try:
        classification = json.loads(raw_response)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Could not parse model response as JSON: {e}\nRaw: {raw_response}")
    if not isinstance(classification, dict):
        raise RuntimeError(
            f"Model response is JSON but not an object mapping ids to colorIds: {raw_response!r}"
        )
    return classification


def _apply_classification(events: list, classification: dict, logger) -> tuple[list, list]:
    """Recolor each event per the classification, which is keyed by the event's
    1-based position in `events` (see _classify_input). An event whose
    classified color is missing/invalid, or whose patch fails, is skipped
    (logged) rather than failing the run — one bad classification shouldn't lose
    the rest. Returns (updated, skipped): updated as (summary, color_id),
    skipped as summaries."""
    updated, skipped = [], []
    for n, event in enumerate(events, 1):
        event_id = event["id"]
        color_id = classification.get(str(n))
        if color_id not in VALID_COLOR_IDS:
            logger.warning(f"No valid color for event {n} ({event['summary']!r}), skipping")
            skipped.append(event["summary"])
            continue

        result = set_event_color(event_id, color_id)
        logger.info(f"set_event_color({event_id}, {color_id}) -> {result}")
        if "error" in result:
            skipped.append(event["summary"])
        else:
            updated.append((event["summary"], color_id))
    return updated, skipped


def main() -> int:
    logger = setup_logger("calendar_colorizer")
    logger.info("Starting calendar colorizer run")

    try:
        start, end = _yesterday_range()
        logger.info(f"Yesterday's range: {start.isoformat()} to {end.isoformat()}")

        events_result = get_events_in_range(start.isoformat(), end.isoformat())
        logger.info(f"get_events_in_range -> {events_result}")
        if "error" in events_result:
            raise RuntimeError(f"get_events_in_range failed: {events_result['error']}")

        events = [
            e for e in events_result.get("events", [])
            if e.get("summary") and e.get("status") != "cancelled"
            # tasks/claude_time_blocks.py logged these hours earlier the same
            # morning, already colored. This run always re-classifies — even
            # events colored by a previous run or by hand — so without this skip
            # it would guess a category from the title and overwrite that color.
            # Scoped to that one prefix on purpose: Strava's events also carry a
            # source_id and should keep being classified.
            and not (e.get("source_id") or "").startswith(SESSION_BLOCK_SOURCE_PREFIX)
        ]

        if not events:
            logger.info("No events to color yesterday — nothing to do")
            logger.info("Colorizer run complete: 0 updated, 0 skipped")
            return 0

        # Nothing else runs the model near 5pm and keep_alive is 30m, so this
        # task always finds it cold. Pre-load it here — after the no-events
        # return above, so a quiet day doesn't pay the ~17GB load for nothing —
        # rather than letting the load and the prefill stack inside the streamed
        # call's read timeout.
        backend = resolve_backend("calendar_colorizer")
        warm_model(logger=logger, backend=backend)
        raw_response = complete_text(
            system_prompt=CLASSIFY_SYSTEM_PROMPT,
            user_prompt=json.dumps(_classify_input(events)),
            backend=backend,
            think=False,   # picking a colorId per title needs no chain-of-thought
            logger=logger,  # surfaces loop.py's num_predict cut-off warning
        )
        logger.info(f"Raw classification response: {raw_response}")

        classification = _parse_classification(raw_response)
        updated, skipped = _apply_classification(events, classification, logger)

        logger.info(f"Colorizer run complete: {len(updated)} updated, {len(skipped)} skipped")
        return 0
    except Exception as e:
        logger.exception(f"Calendar colorizer run failed: {e}")
        notify_failure("calendar_colorizer", e, logger)
        try:
            send_email(
                subject="Calendar colorizer run failed",
                body=f"tasks.calendar_colorizer raised an exception:\n\n{e}",
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
