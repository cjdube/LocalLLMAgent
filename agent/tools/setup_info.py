"""Read-only access to Wren's OWN configuration, so she can answer "what colour
do we use for meal prep" from the table she already colours the calendar with.

The prompting question: asked what colour meal prep events use, Wren said she
had no record of one. She was wrong. `config/settings.json` has held the answer
since the calendar categories were first written — Meal Prep is colorId 10,
"Basil" — and `agent/tools/calendar.py` builds CATEGORY_COLORS out of that same
table at import. Nothing ever showed it to the model. She was blind to her own
settings.

The alternative fixes were considered and rejected. Copying the eleven colours
into config/wren_memory.json would spend most of the 1500-char active-memory
block on facts that go stale the moment the user edits /settings, and the wiki
has the same duplication problem one repo further away. The authoritative copy
already exists and is already loaded; it only needed a reader.

WHAT THIS MAY READ, AND WHY THE LINE IS WHERE IT IS
---------------------------------------------------
The settings document has two halves. `preferences` holds the structured
personal sections — who the user is, his calendar categories, his teams, his
job-search terms. `values` holds the flat schema rows, and that is where every
credential lives (schema.py marks them `secret=True` so their values never leave
the process).

This module reads the `preferences` half ONLY, through schema.PREFERENCE_SECTIONS.
That allowlist is the entire security argument: the preferences half is personal
data by construction, the values half holds keys. **Do not generalise this into a
settings reader.** A tool that took a key name and returned its value would be
one model mistake away from putting an API token in a chat reply, and the model
is the least trusted part of the loop — chat turns ingest untrusted web and mail
content inline. tests/test_setup_info.py asserts the boundary with a fake
credential planted in a fixture.

Sections are read through agent/prefs.py rather than off disk, so a save from
the /settings page reaches this without a restart (prefs.reload() rebinds PREFS).

`_comment` keys are stripped. They are notes to whoever hand-edits the JSON —
"Find a team's id with: ..." — and to the model they read as instructions about
a task it was not asked to do.

Usage:
    python -m agent.tools.setup_info
"""

import argparse
import sys

from agent import config, prefs, schema
from agent.tools._http import load_env, print_result

_NAME = prefs.user_name()

load_env()


def _strip_comments(value):
    """`value` without the `_comment...` keys the JSON carries for human editors.

    Recursive because they appear at both levels — the sports section has two at
    the top and the learnings section comments its inner lists. They are a third
    of the learnings section by size and none of it is an answer to a question.
    """
    if isinstance(value, dict):
        return {k: _strip_comments(v) for k, v in value.items()
                if not k.startswith("_comment")}
    if isinstance(value, list):
        return [_strip_comments(v) for v in value]
    return value


def describe_setup() -> dict:
    """Every non-secret preference section, plus the timezone.

    No arguments on purpose. The whole payload is ~3KB against the 8000-char
    tool-result cap in agent/loop.py, so there is nothing to narrow and nothing
    for a small model to get wrong — a `section` argument would only add a way
    to ask for the wrong one and get an empty answer back.

    Sections are returned as they are stored. The shapes are already the ones
    the user edits on the /settings page, so a reshaping here would be a second
    description of the same data, free to drift from the first.
    """
    sections = {name: _strip_comments(prefs.section(name))
                for name in schema.PREFERENCE_SECTIONS}
    sections = {name: value for name, value in sections.items() if value}
    return {
        "settings": sections,
        "timezone": config.getenv("TIMEZONE", "America/New_York"),
        "note": (
            "These are the settings as they stand right now. They are not fixed "
            f"— {_NAME} can change any of them on Wren's /settings page."
        ),
    }


# The description carries the whole design. Nothing about these values is in the
# system prompt (that was the point — the prompt is already crowded), so this
# text is the only thing standing between a question and a fabricated answer.
# Written to the rule in AGENTS.md: a tool that answers "what is this set to?"
# must say the answer is NOT in the model's head, or the model supplies a
# plausible one and never calls the tool. The failure being prevented is
# specific and was observed: asked what colour meal prep events use, Wren
# answered that she had no record of it.
#
# The everyday phrasings are named on purpose ("what colour do we use for...").
# The model reaches this tool through a load_tools hop, and it will not make
# that hop for a question it does not recognise as a settings question.
DESCRIBE_SETUP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "describe_setup",
        "description": (
            f"Read how {_NAME} has actually configured Wren: his calendar "
            "categories and the colour each kind of event uses, how far ahead "
            "the morning brief looks, which sports teams he follows, what the "
            "daily learnings reviews ignore, his job-search terms, and his "
            "timezone. "
            "**These values are NOT something you know, and you must never "
            "guess one** — they are settings he can change at any time, and a "
            "guessed colour or team looks exactly like a real one. Call this "
            "tool whenever he asks what something is set to, what colour a kind "
            "of event uses or should use, which categories exist, or how Wren "
            "is set up. Only what this tool returns is real. If what he asked "
            "about is not in the result, say it is not configured — do not fill "
            "it in from your own knowledge."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

SETUP_INFO_TOOL_SCHEMAS = [DESCRIBE_SETUP_SCHEMA]


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    return print_result(describe_setup())


if __name__ == "__main__":
    sys.exit(main())
