"""Tests for tasks/calendar_colorizer.py's pure logic — the model-response
parsing and the classify-and-apply accounting. The Google Calendar patch call
(set_event_color) is monkeypatched; nothing touches the network."""

import logging

import pytest

from agent.tools.calendar import CATEGORY_COLORS
from tasks import calendar_colorizer as cc

_LOGGER = logging.getLogger("test_calendar_colorizer")

WORK = CATEGORY_COLORS["Work/LLC"][0]
FITNESS = CATEGORY_COLORS["Fitness"][0]


# --------------------------------------------------------------------------- #
# _parse_classification
# --------------------------------------------------------------------------- #

def test_parse_valid_object():
    assert cc._parse_classification('{"abc": "1", "def": "6"}') == {"abc": "1", "def": "6"}


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

    classification = {"e1": WORK, "e2": "99"}  # e2 invalid colorId, e3 unclassified
    updated, skipped = cc._apply_classification(_events(), classification, _LOGGER)

    assert updated == [("Sprint planning", WORK)]
    assert skipped == ["Morning run", "Mystery block"]
    assert patched == [("e1", WORK)]  # invalid/missing never reach the API


def test_apply_counts_patch_failure_as_skipped(monkeypatch):
    monkeypatch.setattr(cc, "set_event_color",
                        lambda eid, cid: {"error": "API exploded"} if eid == "e1"
                        else {"updated": True})

    classification = {"e1": WORK, "e2": FITNESS, "e3": FITNESS}
    updated, skipped = cc._apply_classification(_events(), classification, _LOGGER)

    assert skipped == ["Sprint planning"]
    assert updated == [("Morning run", FITNESS), ("Mystery block", FITNESS)]


def test_valid_color_ids_derive_from_category_colors():
    # The validation set must track the single source of truth, not a copy.
    assert cc.VALID_COLOR_IDS == {cid for cid, _ in CATEGORY_COLORS.values()}
