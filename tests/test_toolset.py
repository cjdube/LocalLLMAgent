"""Tests for agent/toolset.py — the shared tool registry, its policy sets, the
model-facing send_email hardening, and the confirmation describers used by both
the chat card and the background approval push."""

import importlib
import pkgutil

import agent.tools
from agent import toolset
from agent.tools import email as email_mod


# Tool modules that define a model-facing schema on purpose but keep it out of
# the registry. Empty by design: a schema written at the model and never
# registered is dead code the partition test below cannot see, because that test
# partitions TOOLS and an unregistered schema never enters TOOLS. This is how
# fetch_liked_videos sat unreachable in chat from 5c40332 until 2026-08-03.
# Adding a name here is a deliberate "task-only, and the schema stays" choice —
# the usual answer is to register the tool or delete the schema.
TASK_ONLY_SCHEMAS: set[str] = set()


def test_every_model_facing_schema_is_registered():
    """Every *_SCHEMA under agent/tools/ is in TOOLS, or explicitly allowlisted."""
    unregistered = {}
    registered = {t["function"]["name"] for t in toolset.TOOLS}
    for info in pkgutil.iter_modules(agent.tools.__path__):
        mod = importlib.import_module(f"agent.tools.{info.name}")
        for attr in dir(mod):
            if not attr.endswith("_SCHEMA"):
                continue
            schema = getattr(mod, attr)
            # Tool schemas only — skip unrelated constants ending in _SCHEMA.
            if not (isinstance(schema, dict) and "function" in schema):
                continue
            name = schema["function"]["name"]
            if name not in registered and name not in TASK_ONLY_SCHEMAS:
                unregistered[name] = f"agent.tools.{info.name}.{attr}"
    assert not unregistered, (
        f"model-facing schemas missing from TOOLS: {unregistered}. Register the "
        "tool (TOOLS + DISPATCH + CORE_TOOL_NAMES or a TOOL_GROUP_NAMES group), "
        "delete the unused schema, or allowlist it in TASK_ONLY_SCHEMAS."
    )


# --------------------------------------------------------------------------- #
# Registry invariants: a schema without a dispatch entry surfaces only as a
# runtime "unknown tool" error to the model, and a policy set naming a
# nonexistent tool silently gates nothing.
# --------------------------------------------------------------------------- #

def test_every_tool_schema_has_a_dispatch_entry():
    names = {t["function"]["name"] for t in toolset.TOOLS}
    assert names <= set(toolset.DISPATCH)


def test_policy_sets_name_only_real_tools():
    assert toolset.WRITE_TOOLS <= set(toolset.DISPATCH)
    assert toolset.CONSEQUENTIAL_TOOLS <= toolset.WRITE_TOOLS
    assert toolset.UNATTENDED_EXCLUDED_TOOLS <= set(toolset.DISPATCH)


# --------------------------------------------------------------------------- #
# Lazy tool loading: the core + groups split must partition TOOLS exactly (plus
# the load_tools meta-tool), so a newly-added tool can never become unreachable
# in chat, and no schema is duplicated across groups.
# --------------------------------------------------------------------------- #

def _names(schemas):
    return [s["function"]["name"] for s in schemas]


def test_core_and_groups_partition_the_registry():
    core = set(_names(toolset.CORE_TOOLS))
    grouped = [n for names in toolset.TOOL_GROUP_NAMES.values() for n in names]
    # load_tools is core-only and not a real registry tool.
    assert "load_tools" in core
    assert (core - {"load_tools"}) | set(grouped) == {t["function"]["name"] for t in toolset.TOOLS}
    # No tool is in two places: core and groups are disjoint, groups don't overlap.
    assert (core - {"load_tools"}).isdisjoint(grouped)
    assert len(grouped) == len(set(grouped))


def test_load_tools_schema_enum_matches_the_groups():
    enum = toolset.LOAD_TOOLS_SCHEMA["function"]["parameters"]["properties"]["group"]["enum"]
    assert set(enum) == set(toolset.TOOL_GROUPS)
    # The meta-tool is not itself in the executable registry.
    assert "load_tools" not in {t["function"]["name"] for t in toolset.TOOLS}


def test_tools_for_returns_core_only_for_no_groups():
    names = _names(toolset.tools_for(set()))
    assert names == _names(toolset.CORE_TOOLS)  # order-stable, core first
    assert "load_tools" in names


def test_tools_for_appends_group_and_dedupes():
    names = _names(toolset.tools_for({"wiki"}))
    assert "search_wiki" in names
    assert names[: len(toolset.CORE_TOOLS)] == _names(toolset.CORE_TOOLS)  # core stays first
    assert len(names) == len(set(names))  # no dup even if a group re-listed a core tool


def test_groups_for_message_matches_on_word_boundaries():
    assert "opportunities" in toolset.groups_for_message("any new job openings?")
    assert "activity" in toolset.groups_for_message("how were my strava runs")
    assert "wiki" in toolset.groups_for_message("what have I been learning about?")
    assert "activity" in toolset.groups_for_message("what did I like on youtube?")
    # "liked"/"video" are the YouTube cues, not "like"/"watch": \blike would fire
    # on "likely", and "watch" belongs to opportunities (watchlist).
    assert "activity" not in toolset.groups_for_message("is that likely to work")
    assert "activity" not in toolset.groups_for_message("what's on my watchlist")
    # No cues -> no groups (a plain weather ask stays core-only).
    assert toolset.groups_for_message("what's the temperature outside") == set()


def test_asking_about_your_own_notes_loads_the_wiki():
    # Both of these reached the live server without loading the wiki group, so
    # Wren searched her memory store and reported nothing written down about
    # topics that have wiki pages. Saying "wiki" out loud was the only way in.
    assert "wiki" in toolset.groups_for_message("what have I written down about pricing?")
    assert "wiki" in toolset.groups_for_message("what do my notes say about GroceryGuru?")
    assert "wiki" in toolset.groups_for_message("what did I write down about lenses")
    assert "wiki" in toolset.groups_for_message("what have I read about agent memory")
    # "learning" as a cue missed the commonest phrasing of all; "learn" catches
    # learn/learned/learning/learnings.
    assert "wiki" in toolset.groups_for_message("what have I learned about RAG?")
    assert "wiki" in toolset.groups_for_message("what have I been learning about?")


def test_render_toolgroups_index_lists_every_group():
    index = toolset.render_toolgroups_index()
    for group in toolset.TOOL_GROUPS:
        assert group in index
    assert "load_tools" in index


# --------------------------------------------------------------------------- #
# send_email hardening: the dispatch entry accepts exactly what the schema
# declares. The agent loop forwards whatever arguments the model emits, so a
# hallucinated or injected to=/html= must be dropped, not silently honored.
# --------------------------------------------------------------------------- #

def test_email_schema_declares_only_subject_and_body():
    props = email_mod.TOOL_SCHEMA["function"]["parameters"]["properties"]
    assert set(props) == {"subject", "body"}


def test_model_facing_send_email_drops_undeclared_args(monkeypatch):
    seen = {}

    def fake_send(subject, body, to=None, html=False):
        seen.update(subject=subject, body=body, to=to, html=html)
        return {"message_id": "m1"}

    monkeypatch.setattr(email_mod, "send_email", fake_send)
    result = toolset.DISPATCH["send_email"](
        subject="Hi", body="the body", to="attacker@evil.com", html=True
    )
    assert result == {"message_id": "m1"}
    assert seen["subject"] == "Hi" and seen["body"] == "the body"
    # The injected recipient and format never reach the real sender; the
    # recipient stays send_email's own default (BRIEF_TO_EMAIL).
    assert seen["to"] is None
    assert seen["html"] is False


# --------------------------------------------------------------------------- #
# Confirmation describers
# --------------------------------------------------------------------------- #

def _email_call(**args) -> dict:
    return {"function": {"name": "send_email", "arguments": args}}


def test_email_summary_shows_the_effective_recipient(monkeypatch):
    monkeypatch.setenv("BRIEF_TO_EMAIL", "owner@example.com")
    text = toolset.describe_call(_email_call(subject="Hi", body="b"))
    assert "owner@example.com" in text and '"Hi"' in text


def test_email_summary_never_shows_an_injected_recipient(monkeypatch):
    # Even if the model emitted to=, the wrapper won't honor it — and the
    # approval surface must describe what will actually happen.
    monkeypatch.setenv("BRIEF_TO_EMAIL", "owner@example.com")
    text = toolset.describe_call(
        _email_call(subject="Hi", body="b", to="attacker@evil.com")
    )
    assert "attacker@evil.com" not in text
    assert "owner@example.com" in text


def test_email_detail_includes_body_with_tags_stripped():
    detail = toolset.describe_call_detail(
        _email_call(subject="Hi", body="First line\n<b>bold</b> and more")
    )
    assert "First line" in detail and "bold" in detail
    assert "<b>" not in detail  # stray tags stripped defensively


def test_email_detail_is_truncated():
    detail = toolset.describe_call_detail(_email_call(body="word " * 200))
    assert len(detail) <= toolset.BODY_PREVIEW_CHARS + 1  # +1 for the ellipsis
    assert detail.endswith("…")


def test_non_email_write_has_no_detail():
    call = {"function": {"name": "log_calendar_event", "arguments": {"summary": "x"}}}
    assert toolset.describe_call_detail(call) is None


def test_unknown_tool_falls_back_to_generic_description():
    call = {"function": {"name": "mystery_tool", "arguments": {"x": 1}}}
    assert toolset.describe_call(call) == 'mystery_tool({"x": 1})'


# --------------------------------------------------------------------------- #
# Memory writes are confirm-gated: chat ingests untrusted web/search content
# inline, and a pinned fact is injected into every future system prompt, so an
# injected "pin that ..." must pause for the user's tap instead of auto-executing.
# --------------------------------------------------------------------------- #

def test_memory_writes_are_confirm_gated():
    assert {"remember", "pin", "recategorize"} <= toolset.WRITE_TOOLS


def test_every_write_tool_has_a_specific_describer():
    # Every confirm-gated tool must render a purpose-built card, not the generic
    # "name(json)" fallback: a blank or wrong confirmation summary for a write
    # action would let it slip through unnoticed on the tap-to-approve surface.
    # Table-driven over WRITE_TOOLS so a newly-gated tool without a describer
    # fails here instead of shipping a mystery card.
    for name in sorted(toolset.WRITE_TOOLS):
        summary = toolset.describe_call({"function": {"name": name, "arguments": {}}})
        assert summary and not summary.startswith(f"{name}("), name


def test_write_describers_read_cleanly():
    def summary(name, **args):
        return toolset.describe_call({"function": {"name": name, "arguments": args}})

    # Memory writes (gated in S1).
    assert "Remember" in summary("remember", text="Crows hold grudges") \
        and "Crows hold grudges" in summary("remember", text="Crows hold grudges")
    assert "Pin" in summary("pin", text="I prefer metric") \
        and "I prefer metric" in summary("pin", text="I prefer metric")
    recat = summary("recategorize", memory_id="a1b2c3d4", category="preference")
    assert "a1b2c3d4" in recat and "preference" in recat

    # Reminder and background writes (describers added alongside this test).
    rem = summary("set_reminder", message="Call the dentist", when="tomorrow 9am")
    assert "Call the dentist" in rem and "tomorrow 9am" in rem
    assert "z9y8x7" in summary("cancel_reminder", reminder_id="z9y8x7")
    assert "research Acme" in summary("run_in_background", task="research Acme")


def test_run_in_background_describer_truncates_a_long_task():
    long_task = "investigate " * 40
    summary = toolset.describe_call(
        {"function": {"name": "run_in_background", "arguments": {"task": long_task}}})
    assert summary.endswith('…"')
    assert len(summary) < len(long_task)


# --------------------------------------------------------------------------- #
# reply_to_thread: the one tool that mails someone other than the user. Its
# recipients are not in its arguments — they come from the thread's headers — so
# the confirmation card has to go and read them, and these tests are what keep
# the card honest about who is about to be emailed.
# --------------------------------------------------------------------------- #

def _reply_call(**args) -> dict:
    return {"function": {"name": "reply_to_thread", "arguments": args}}


def test_reply_schema_declares_only_thread_id_and_body():
    # No `to`, no `cc`. There is nothing for an injected address to land in.
    props = email_mod.REPLY_TO_THREAD_TOOL_SCHEMA["function"]["parameters"]["properties"]
    assert set(props) == {"thread_id", "body"}


def test_reply_is_gated_in_chat_and_in_the_background():
    # Both halves: WRITE_TOOLS is the chat card, CONSEQUENTIAL_TOOLS is the
    # phone approval a background run must get. A reply in his name to a real
    # contact needs both, and listing it in only one would look fine.
    assert "reply_to_thread" in toolset.WRITE_TOOLS
    assert "reply_to_thread" in toolset.CONSEQUENTIAL_TOOLS


def test_reply_summary_names_the_people_it_will_actually_go_to(monkeypatch):
    monkeypatch.setattr(toolset, "reply_plan", lambda tid: {
        "to": ["Dana Fox <dana@acme.com>", "Sam <sam@acme.com>"],
        "subject": "Re: Walkthrough", "in_reply_to": "", "references": ""})

    text = toolset.describe_call(_reply_call(thread_id="t1", body="ok"))

    assert "dana@acme.com" in text and "sam@acme.com" in text


def test_reply_summary_gives_the_real_reason_there_are_no_recipients(monkeypatch):
    """A card that silently omits the recipients is worse than one that admits
    it does not know them — he can still deny it. And the reason must be the
    real one: a mailing list refusal read fine, so "could not be read" would
    send him looking for a Gmail outage that isn't there."""
    monkeypatch.setattr(toolset, "reply_plan",
                        lambda tid: {"error": "26 people are on it — reply in Gmail instead"})

    text = toolset.describe_call(_reply_call(thread_id="t1", body="ok"))

    assert "reply in Gmail instead" in text
    assert "could not be read" not in text


def test_reply_summary_survives_a_raising_thread_read(monkeypatch):
    def boom(tid):
        raise RuntimeError("gmail down")
    monkeypatch.setattr(toolset, "reply_plan", boom)

    assert "could not be read" in toolset.describe_call(_reply_call(thread_id="t1"))


def test_reply_summary_reads_no_thread_when_there_is_no_thread_id(monkeypatch):
    """describe_call runs on the confirmation path, and the describer table test
    calls it with empty arguments — neither may reach Gmail."""
    def boom(tid):
        raise AssertionError("reply_plan reached without a thread id")
    monkeypatch.setattr(toolset, "reply_plan", boom)

    assert toolset.describe_call(_reply_call())


def test_reply_detail_shows_the_body_being_sent():
    detail = toolset.describe_call_detail(_reply_call(thread_id="t1", body="Thursday works."))
    assert detail == "Thursday works."
