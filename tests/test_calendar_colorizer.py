"""Tests for tasks/calendar_colorizer.py's pure logic — the model-response
parsing and the classify-and-apply accounting. The Google Calendar patch call
(set_event_color) is monkeypatched; nothing touches the network."""

import logging

import pytest

from agent.tools.calendar import CATEGORY_COLORS
from chat.insights import _is_run_success
from tasks import calendar_colorizer as cc

_LOGGER = logging.getLogger("test_calendar_colorizer")


def _never_called(*a, **k):
    raise AssertionError("the model must not be warmed on a day with no events")

WORK = CATEGORY_COLORS["Work/LLC"][0]
FITNESS = CATEGORY_COLORS["Fitness"][0]


# --------------------------------------------------------------------------- #
# _parse_classification
# --------------------------------------------------------------------------- #

def test_parse_valid_object():
    assert cc._parse_classification('{"1": "1", "2": "6"}') == {"1": "1", "2": "6"}


@pytest.mark.parametrize("raw", ["", "   \n"])
def test_parse_reports_empty_response_distinctly(raw):
    # A thinking model that burns its whole num_predict budget reasoning returns
    # no content; "bad JSON" is a misleading diagnosis for that.
    with pytest.raises(RuntimeError, match="empty response"):
        cc._parse_classification(raw)


@pytest.mark.parametrize("raw", [
    "Sure! Here's the mapping: {\"abc\": \"1\"}",   # prose preamble
    '```json\n{"abc": "1"}\n```',                    # code fences
    "not json at all",
])
def test_parse_rejects_non_json(raw):
    with pytest.raises(RuntimeError, match="Could not parse"):
        cc._parse_classification(raw)


def test_parse_rejects_json_that_is_not_an_object():
    with pytest.raises(RuntimeError, match="not an object"):
        cc._parse_classification('["1", "6"]')


# --------------------------------------------------------------------------- #
# _apply_classification
# --------------------------------------------------------------------------- #

def _events():
    return [
        {"id": "e1", "summary": "Sprint planning"},
        {"id": "e2", "summary": "Morning run"},
        {"id": "e3", "summary": "Mystery block"},
    ]


def test_apply_updates_valid_and_skips_missing_or_invalid(monkeypatch):
    patched = []
    monkeypatch.setattr(cc, "set_event_color",
                        lambda eid, cid: (patched.append((eid, cid)) or {"updated": True}))

    classification = {"1": WORK, "2": "99"}  # 2 invalid colorId, 3 unclassified
    updated, skipped = cc._apply_classification(_events(), classification, _LOGGER)

    assert updated == [("Sprint planning", WORK)]
    assert skipped == ["Morning run", "Mystery block"]
    assert patched == [("e1", WORK)]  # invalid/missing never reach the API


def test_apply_counts_patch_failure_as_skipped(monkeypatch):
    monkeypatch.setattr(cc, "set_event_color",
                        lambda eid, cid: {"error": "API exploded"} if eid == "e1"
                        else {"updated": True})

    classification = {"1": WORK, "2": FITNESS, "3": FITNESS}
    updated, skipped = cc._apply_classification(_events(), classification, _LOGGER)

    assert skipped == ["Sprint planning"]
    assert updated == [("Morning run", FITNESS), ("Mystery block", FITNESS)]


def test_quiet_day_still_logs_a_run_complete_boundary(monkeypatch, capsys):
    # The dashboard reads run status from the log: a run that logs a start and no
    # completion is reported as still "running" forever. The nothing-to-color
    # early return used to do exactly that. Asserted through insights' own
    # matcher so the two can't drift apart.
    monkeypatch.setattr(cc, "get_events_in_range", lambda *a, **k: {"events": []})
    monkeypatch.setattr(cc, "complete_text", _never_called)
    monkeypatch.setattr(cc, "notify_failure", lambda *a, **k: None)

    assert cc.main() == 0
    assert any(_is_run_success(line) for line in capsys.readouterr().out.splitlines())


def test_classify_input_hides_event_ids_behind_numbers():
    # Google's 26-char ids sent to the model cost a run: it burned its whole
    # token budget transcribing one, and mis-copied it even when it succeeded.
    # Python owns the number -> id mapping now.
    assert cc._classify_input(_events()) == [
        {"n": 1, "summary": "Sprint planning"},
        {"n": 2, "summary": "Morning run"},
        {"n": 3, "summary": "Mystery block"},
    ]


def test_apply_maps_numbers_back_to_the_right_event_ids(monkeypatch):
    patched = []
    monkeypatch.setattr(cc, "set_event_color",
                        lambda eid, cid: (patched.append((eid, cid)) or {"updated": True}))

    cc._apply_classification(_events(), {"1": WORK, "3": FITNESS}, _LOGGER)

    assert patched == [("e1", WORK), ("e3", FITNESS)]


def test_valid_color_ids_derive_from_category_colors():
    # The validation set must track the single source of truth, not a copy.
    assert cc.VALID_COLOR_IDS == {cid for cid, _ in CATEGORY_COLORS.values()}
