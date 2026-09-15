"""Tests for agent/tools/setup_info.py.

Two things are being protected, and only one of them is the feature.

The feature: Wren can read her own configuration, so the question that exposed
the gap ("what colour do we use for meal prep") has an answer. That is the easy
half, and it is asserted against the shipped example file rather than a
hand-built fixture, so a category renamed in config/preferences.example.json
shows up here rather than in a chat reply.

The boundary: the settings document has a `values` half holding every credential
(schema.py marks them secret=True), and this tool reads the `preferences` half
only. That is one allowlist away from leaking an API token into a chat reply,
and the model is the least trusted part of the loop. The credential tests below
plant a real-shaped secret and assert it never surfaces — including through the
paths that would carry it accidentally, which is where a leak would actually
come from.
"""

import json

import pytest

from agent import prefs, schema
from agent.tools import setup_info


# ---- the feature -----------------------------------------------------------

def test_the_meal_prep_colour_comes_back():
    """The exact question that exposed the gap. Wren answered that she had no
    record of a colour for meal prep, from a table she colours the calendar
    with every day, because nothing could read it."""
    categories = setup_info.describe_setup()["settings"]["calendar"]["categories"]
    meal_prep = [c for c in categories if c["name"].lower() == "meal prep"]
    assert meal_prep, "no Meal Prep category in the shipped calendar section"
    assert meal_prep[0]["color_id"], "a category with no colorId answers nothing"
    assert meal_prep[0]["color_name"], "the colour NAME is what he sees in Google Calendar"


def test_every_category_carries_both_halves_of_the_answer():
    """The user asked for name + id together: the colour name is what he sees in
    Google Calendar, the id is what the colorizer writes. One without the other
    is half an answer, and the half depends on which he is doing."""
    for category in setup_info.describe_setup()["settings"]["calendar"]["categories"]:
        assert category.get("color_name"), f"{category['name']} has no colour name"
        assert category.get("color_id"), f"{category['name']} has no colorId"


def test_every_preference_section_reaches_the_model():
    """The allowlist IS the contract, in both directions. A section added to
    schema.PREFERENCE_SECTIONS later should appear here automatically — if it
    does not, this tool has quietly stopped describing part of the setup."""
    result = setup_info.describe_setup()["settings"]
    for name in schema.PREFERENCE_SECTIONS:
        if prefs.section(name):
            assert name in result, f"{name} is configured but never reaches the model"


def test_the_timezone_is_there():
    """Not a preference section — it is a schema row — so it is the one value
    that has to be added by hand and the one that can silently go missing."""
    assert setup_info.describe_setup()["timezone"]


def test_the_result_says_the_settings_can_change():
    """Without this the model presents a snapshot as a permanent fact. They are
    settings; he edits them on a page."""
    assert "/settings" in setup_info.describe_setup()["note"]


def test_the_result_fits_the_tool_result_cap():
    """agent/loop.py caps a tool result at 8000 chars and this tool has no
    TOOL_RESULT_CHAR_CAPS entry, which is only correct while the payload stays
    comfortably under. A silently trimmed result reads as complete to the model.
    Measured 2,872 chars on the live document; this bites long before 8000."""
    assert len(json.dumps(setup_info.describe_setup())) < 6000


# ---- the boundary ----------------------------------------------------------

_SECRET = "sk-live-DO-NOT-LEAK-4a91f2"


@pytest.fixture
def planted_secret(monkeypatch):
    """A settings document shaped like the real one, with a credential in the
    `values` half and a preference section beside it."""
    monkeypatch.setattr(prefs, "PREFS", {
        "persona": {"user_name": "Testy"},
        "calendar": {"categories": [
            {"name": "Meal Prep", "color_id": "10", "color_name": "Basil"},
        ]},
    })
    monkeypatch.setitem(
        __import__("agent.config", fromlist=["CONFIG"]).CONFIG,
        "values",
        {"CLICKUP_API_TOKEN": _SECRET, "NTFY_TOKEN": _SECRET},
    )
    return _SECRET


def test_no_credential_reaches_the_model(planted_secret):
    """The whole security argument in one assertion. Serialised, because a leak
    would not politely appear as a top-level key — it would be nested inside
    whatever carried it."""
    assert planted_secret not in json.dumps(setup_info.describe_setup())


def test_the_values_half_is_never_returned(planted_secret):
    """The shape of the leak, not just the string. `values` is where every
    credential lives, so its presence is the thing to refuse — including a
    future credential this test does not know the name of."""
    result = setup_info.describe_setup()
    assert "values" not in result
    assert "values" not in result["settings"]
    assert "CLICKUP_API_TOKEN" not in json.dumps(result)


def test_the_secret_test_bites_when_the_allowlist_is_widened(planted_secret, monkeypatch):
    """Proof the two tests above are not green for the wrong reason.

    A test that asserts a secret is absent passes trivially if the secret was
    never anywhere near the code. So: widen the allowlist the way a careless
    "improvement" would — read the whole settings document instead of the
    preference sections — and confirm the assertion fails. If this test ever
    stops failing-then-passing, the ones above have stopped proving anything.
    """
    import agent.config

    def leaky():
        return {"settings": agent.config.CONFIG, "timezone": "UTC", "note": ""}

    monkeypatch.setattr(setup_info, "describe_setup", leaky)
    assert _SECRET in json.dumps(setup_info.describe_setup()), \
        "the leaky version did not leak — this test is no longer proving anything"


# ---- comment stripping -----------------------------------------------------

def test_the_json_comments_are_stripped():
    """`_comment` keys are notes to whoever hand-edits the JSON — one of them
    is a shell command to run. To the model they read as instructions about a
    task it was not asked to do, and they are a third of the learnings section
    by size."""
    assert "_comment" not in json.dumps(setup_info.describe_setup())


def test_comments_are_stripped_at_every_depth():
    """They appear at both levels — the sports section comments at the top, the
    learnings section comments its inner lists — so a one-level strip would
    leave half of them in."""
    nested = {"a": 1, "_comment": "x", "b": {"_comment_find": "y", "c": [{"_comment": "z", "d": 2}]}}
    assert setup_info._strip_comments(nested) == {"a": 1, "b": {"c": [{"d": 2}]}}


# ---- the description is the whole design -----------------------------------

def test_the_description_denies_pretraining():
    """Nothing about these values is in the system prompt — that was the point,
    the prompt is already crowded — so this text is the only thing between a
    question and a fabricated answer. AGENTS.md's catalogue rule: a tool that
    answers "what is this set to?" must say the answer is NOT in the model's
    head, or the model supplies a plausible one and never calls the tool."""
    description = setup_info.DESCRIBE_SETUP_SCHEMA["function"]["description"]
    assert "not something you know" in description.lower()
    assert "never guess" in description.lower()


def test_the_description_names_the_everyday_asks():
    """The model reaches this tool through a load_tools hop and will not make
    that hop for a question it does not recognise as a settings question.
    "colour" is the word in the ask that failed."""
    description = setup_info.DESCRIBE_SETUP_SCHEMA["function"]["description"].lower()
    assert "colour" in description or "color" in description
    assert "set to" in description


def test_the_tool_takes_no_arguments():
    """Deliberate: the whole payload fits one result, so there is nothing to
    narrow and nothing for a small model to get wrong."""
    assert not setup_info.DESCRIBE_SETUP_SCHEMA["function"]["parameters"]["properties"]
