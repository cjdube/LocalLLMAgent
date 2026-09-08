"""Tests for agent.session_ticket — filing a Claude Code session as a ClickUp
Task.

Both Claude Code roots are redirected to tmp_path and every ClickUp call is
monkeypatched, so nothing here reads the real ~/.claude or reaches the network.
The transcript fixtures are built from **captured line shapes**, not invented
ones: the noise lines below (the isMeta caveat, the <command-name> wrapper, the
tool_result) are the real first lines of real sessions on this machine, and a
naive "first user line" reader picks one of them instead of the prompt.

The guarantees worth breaking a build over:

1. The prompt found is the one a person typed, past that noise.
2. Create, attach, then move — and a failure after the create still reports the
   Task that now exists. (the ordering and warning tests)
3. Running it twice files one Task, not two. (test_a_second_run_refuses)
4. A cut quote says it was cut. (test_a_long_prompt_is_cut_out_loud)
"""

import json

import pytest

from agent import session_ticket
from agent.tools import clickup

SESSION_ID = "d85adfa4-b4d0-482d-8022-713ec5c8c535"
SLUG = "i-have-a-new-cheerful-rabin"
PROMPT = "I have a new feature idea I would like to build out for Wren."
REPLY = "I will look at two things: the ClickUp watcher, and where the plans live."
PLAN = "# File a session as a ticket\n\n## Context\n\nWhy this exists.\n"


def _user(text, **over):
    """A transcript `user` line. Defaults to a real typed prompt; override
    origin/promptSource/isMeta to make it one of the noise shapes."""
    rec = {
        "type": "user",
        "message": {"role": "user", "content": text},
        "timestamp": "2026-09-08T10:22:41.463Z",
        "origin": {"kind": "human"},
        "promptSource": "sdk",
        "sessionId": SESSION_ID,
        "slug": SLUG,
    }
    rec.update(over)
    return rec


def _assistant(block, request_id="req_1", **over):
    """An assistant line. One content BLOCK per line in a real transcript, and
    one API response spread over several lines sharing a requestId — thinking,
    then text, then a tool_use per call."""
    rec = {
        "type": "assistant",
        "requestId": request_id,
        "message": {"role": "assistant", "content": [block]},
        "timestamp": "2026-09-08T10:22:50.000Z",
        "sessionId": SESSION_ID,
        "slug": SLUG,
    }
    rec.update(over)
    return rec


def _thinking(text="quietly reasoning"):
    return {"type": "thinking", "thinking": text, "signature": "CAIS8gwK"}


def _tool_use(name="Bash"):
    return {"type": "tool_use", "id": "toolu_1", "name": name, "input": {"command": "ls"}}


def _text(text):
    return {"type": "text", "text": text}


def _write_transcript(root, lines, session_id=SESSION_ID, project="-Users-craigdube-Projects-X"):
    folder = root / project
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{session_id}.jsonl"
    # Compact separators, because that is how Claude Code writes these files —
    # a fixture with pretty spacing would let a reader that depends on the exact
    # '"slug":"…"' byte sequence pass here and fail on a real transcript.
    path.write_text(
        "\n".join(json.dumps(line, separators=(",", ":")) for line in lines) + "\n",
        encoding="utf-8")
    return path


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """Point both Claude Code roots at tmp_path and lay down one ordinary
    session: noise first, then the real prompt, then the title lines."""
    projects = tmp_path / "projects"
    plans = tmp_path / "plans"
    projects.mkdir()
    plans.mkdir()
    monkeypatch.setenv("WREN_CLAUDE_PROJECTS_ROOT", str(projects))
    monkeypatch.setenv("WREN_CLAUDE_PLANS_ROOT", str(plans))

    _write_transcript(projects, [
        {"type": "queue-operation", "operation": "enqueue", "sessionId": SESSION_ID},
        _user("<local-command-caveat>Caveat: the messages below…</local-command-caveat>",
              isMeta=True, origin=None, promptSource=None),
        _user("<command-name>/model</command-name>", origin=None, promptSource=None),
        _user([{"type": "tool_result", "content": "ok"}],
              origin=None, promptSource=None, toolUseResult={"ok": True}),
        _user(PROMPT),
        _assistant(_thinking()),
        _assistant(_text(REPLY)),
        _assistant(_tool_use()),
        _assistant(_text("A later turn, after a second prompt."), request_id="req_2"),
        _user("and a second thing I typed later"),
        {"type": "ai-title", "aiTitle": "Some generated title", "sessionId": SESSION_ID},
        {"type": "custom-title", "customTitle": "Session ticket skill", "sessionId": SESSION_ID},
    ])
    (plans / f"{SLUG}.md").write_text(PLAN, encoding="utf-8")
    return {"projects": projects, "plans": plans}


@pytest.fixture
def stub(monkeypatch):
    """Stand in for ClickUp, recording the ORDER of calls — the order is the
    guarantee here, so a stub counting calls would let the wrong one pass."""
    events = []
    state = {"existing": None, "add_error": None, "upload_error": None,
             "move_error": None, "find_error": None}

    def _read(title, api_key=None):
        events.append(("read", title))
        if state["existing"]:
            return {"title": state["existing"], "url": "https://app.clickup.com/t/old"}
        return {"error": f"no ClickUp task matching '{title}'. "
                         "Use list_clickup_tasks to see what exists."}

    def _add(title, space, list_name=None, description=None, tags=None,
             priority=None, api_key=None):
        events.append(("add", title, space, priority, description))
        if state["add_error"]:
            return {"error": state["add_error"]}
        return {"tool_name": "add_clickup_task", "created": True, "title": title,
                "space": space, "list": "Backlog", "status": "idea",
                "url": "https://app.clickup.com/t/abc123"}

    def _find(title, api_key=None):
        events.append(("find", title))
        if state["find_error"]:
            return {"error": state["find_error"]}
        return {"id": "abc123", "title": title}

    def _upload(task_id, filename, data, api_key=None):
        events.append(("upload", task_id, filename, len(data)))
        if state["upload_error"]:
            return {"error": state["upload_error"]}
        return {"attached": filename, "id": "att1", "task_id": task_id}

    def _move(title, status, api_key=None):
        events.append(("move", title, status))
        if state["move_error"]:
            return {"error": state["move_error"]}
        return {"tool_name": "move_clickup_task", "title": title, "status": status}

    monkeypatch.setattr(clickup, "read_clickup_task", _read)
    monkeypatch.setattr(clickup, "add_clickup_task", _add)
    monkeypatch.setattr(clickup, "find_task_id", _find)
    monkeypatch.setattr(clickup, "upload_attachment", _upload)
    monkeypatch.setattr(clickup, "move_clickup_task", _move)
    return {"events": events, "state": state}


def _kinds(stub):
    return [e[0] for e in stub["events"]]


# --------------------------------------------------------------------------- #
# Reading the transcript
# --------------------------------------------------------------------------- #

def test_the_prompt_found_is_the_one_a_person_typed(roots):
    """The three lines before it are the real shapes that fool a naive reader:
    an isMeta caveat, a slash-command wrapper, and a tool result."""
    facts = session_ticket.session_facts(session_id=SESSION_ID)
    assert facts["first_prompt"] == PROMPT


def test_a_task_notification_is_not_a_typed_prompt(roots):
    """The case only the `origin` check catches. It carries ordinary prose, no
    wrapper tag, no isMeta and no tool result, so every other filter in the
    reader lets it straight through — and it is the FIRST user line in the
    sessions that have one. `origin.kind` is `task-notification`, not `human`.
    """
    _write_transcript(roots["projects"], [
        _user("The background task you started has finished.",
              origin={"kind": "task-notification"}, promptSource=None),
        _user(PROMPT),
    ], session_id="notified")
    facts = session_ticket.session_facts(session_id="notified")
    assert facts["first_prompt"] == PROMPT


def test_a_pasted_image_contributes_no_base64(roots):
    """message.content is an array when the prompt carried an image, and that
    array holds a megabyte of base64 beside the text."""
    _write_transcript(roots["projects"], [
        _user([{"type": "image", "source": {"type": "base64", "data": "iVBORw0KGgo" * 500}},
               {"type": "text", "text": "look at this screenshot"}]),
    ], session_id="img")
    facts = session_ticket.session_facts(session_id="img")
    assert facts["first_prompt"] == "look at this screenshot"
    assert "iVBORw0" not in facts["first_prompt"]


def test_the_reply_is_the_answer_to_that_first_prompt(roots):
    facts = session_ticket.session_facts(session_id=SESSION_ID)
    assert facts["first_reply"] == REPLY


def test_thinking_and_tool_calls_are_not_the_reply(roots):
    """Thinking is never shown to the user and a tool_use is a JSON payload.
    Both sit on their own lines inside the same response as the text."""
    facts = session_ticket.session_facts(session_id=SESSION_ID)
    assert "quietly reasoning" not in facts["first_reply"]
    assert "toolu_1" not in facts["first_reply"]
    assert "Bash" not in facts["first_reply"]


def test_a_later_turn_is_not_part_of_the_first_reply(roots):
    """The fixture's second response has its own requestId, which is what ends
    the first one — the reply must be the answer to the FIRST prompt only."""
    facts = session_ticket.session_facts(session_id=SESSION_ID)
    assert "A later turn" not in facts["first_reply"]


def test_one_response_split_across_lines_is_joined(roots):
    """One API response is several transcript lines sharing a requestId, and a
    text block often sits between two tool calls. Stopping at the first
    non-text line would drop the rest of the answer."""
    _write_transcript(roots["projects"], [
        _user(PROMPT),
        _assistant(_text("First I will check the watcher.")),
        _assistant(_tool_use()),
        _assistant(_text("Now the plans folder.")),
    ], session_id="split")
    facts = session_ticket.session_facts(session_id="split")
    assert facts["first_reply"] == "First I will check the watcher.\n\nNow the plans folder."


def test_a_session_with_no_reply_yet_is_still_filed(roots):
    """A prompt answered only by tool calls so far has no prose to quote. That
    is not an error — the ask alone is enough to file."""
    _write_transcript(roots["projects"], [
        _user(PROMPT),
        _assistant(_thinking()),
        _assistant(_tool_use()),
    ], session_id="quiet")
    facts = session_ticket.session_facts(session_id="quiet")
    assert facts["first_reply"] == ""
    body = session_ticket.description_for(facts)
    assert "**Asked**" in body
    assert "Answered" not in body, "an empty quote under a heading reads like a bug"


def test_the_last_custom_title_beats_the_generated_one(roots):
    facts = session_ticket.session_facts(session_id=SESSION_ID)
    assert facts["session_title"] == "Session ticket skill"


def test_the_timestamp_is_converted_to_local_never_sliced(roots, monkeypatch):
    """10:22Z is the 8th in UTC and still the 8th in New York — but 01:30Z is
    the 8th in UTC and the 7th here. Slicing the ISO string gets that wrong."""
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    _write_transcript(roots["projects"], [
        _user(PROMPT, timestamp="2026-09-08T01:30:00.000Z"),
    ], session_id="late")
    facts = session_ticket.session_facts(session_id="late")
    assert facts["started_local"].startswith("2026-09-07")


def test_the_plan_file_reaches_the_same_session_as_the_id(roots):
    by_id = session_ticket.session_facts(session_id=SESSION_ID)
    by_plan = session_ticket.session_facts(plan_path=str(roots["plans"] / f"{SLUG}.md"))
    assert by_plan["transcript"] == by_id["transcript"]
    assert by_plan["first_prompt"] == by_id["first_prompt"]


def test_a_session_with_no_typed_prompt_is_refused(roots):
    _write_transcript(roots["projects"], [
        _user("<command-name>/model</command-name>", origin=None, promptSource=None),
    ], session_id="empty")
    facts = session_ticket.session_facts(session_id="empty")
    assert "no typed prompt" in facts["error"]


def test_a_slug_that_escapes_the_plans_folder_finds_nothing(roots):
    """The slug is text read out of a file, so it is not trusted to build a
    path. A traversing slug must find no plan rather than reading one."""
    outside = roots["plans"].parent / "secret.md"
    outside.write_text("# Not a plan\n", encoding="utf-8")
    _write_transcript(roots["projects"], [
        _user(PROMPT, slug="../secret"),
    ], session_id="escape")
    facts = session_ticket.session_facts(session_id="escape")
    assert facts["plan_path"] == ""


def test_a_subagent_plan_is_not_this_sessions_plan(roots):
    """Subagent plans are a <slug>-agent-<hex>.md variant sitting in the same
    folder. Only the exact slug is this session's plan."""
    (roots["plans"] / f"{SLUG}-agent-a179d34e0b25ad5f5.md").write_text("# Subagent\n",
                                                                      encoding="utf-8")
    facts = session_ticket.session_facts(session_id=SESSION_ID)
    assert facts["plan_path"].endswith(f"{SLUG}.md")
    assert facts["plan_h1"] == "File a session as a ticket"


# --------------------------------------------------------------------------- #
# The quote
# --------------------------------------------------------------------------- #

def test_every_line_of_the_quote_is_a_blockquote():
    block = session_ticket.quote_block("first line\n\nthird line")
    assert block.splitlines() == ["> first line", ">", "> third line"]


def test_a_long_prompt_is_cut_out_loud():
    """ClickUp truncates an over-long description without saying so, which
    leaves a quote ending mid-word and nothing to explain it."""
    block = session_ticket.quote_block("word " * 2000, budget=200)
    assert "cut here" in block
    assert "more characters in the original prompt" in block


def test_a_short_prompt_says_nothing_about_cutting():
    assert "cut here" not in session_ticket.quote_block("short")


def test_the_description_quotes_the_ask_and_the_answer(roots):
    facts = session_ticket.session_facts(session_id=SESSION_ID)
    body = session_ticket.description_for(facts)
    assert f"**Asked**\n\n> {PROMPT}" in body
    assert f"**Answered**\n\n> {REPLY}" in body


def test_the_description_names_the_session_it_came_from(roots):
    facts = session_ticket.session_facts(session_id=SESSION_ID)
    body = session_ticket.description_for(facts)
    assert SLUG in body


def test_both_quotes_together_fit_inside_clickups_own_limit(roots):
    """ClickUp truncates a description over its limit without saying so, which
    would cut the provenance line off the bottom. The two budgets plus the
    labels have to fit under it, so this asserts the arithmetic rather than
    trusting it."""
    facts = dict(session_ticket.session_facts(session_id=SESSION_ID),
                 first_prompt="ask " * 4000, first_reply="answer " * 4000)
    body = session_ticket.description_for(facts)
    assert len(body) < clickup._MAX_NEW_DESCRIPTION_CHARS
    assert SLUG in body


# --------------------------------------------------------------------------- #
# Filing it
# --------------------------------------------------------------------------- #

def test_a_dry_run_writes_nothing(roots, stub):
    out = session_ticket.create_ticket(session_id=SESSION_ID, dry_run=True)
    assert out["dry_run"] is True
    assert out["title"] == "File a session as a ticket"
    assert stub["events"] == []


def test_the_title_comes_from_the_plans_heading(roots, stub):
    out = session_ticket.create_ticket(session_id=SESSION_ID)
    assert out["title"] == "File a session as a ticket"


def test_create_then_attach_then_move(roots, stub):
    """The move is last so the board never says 'designed' while the plan is
    still missing."""
    session_ticket.create_ticket(session_id=SESSION_ID)
    assert _kinds(stub) == ["read", "add", "find", "upload", "move"]


def test_the_priority_defaults_to_normal_and_is_passed_through(roots, stub):
    session_ticket.create_ticket(session_id=SESSION_ID)
    assert stub["events"][1][3] == "normal"
    stub["events"].clear()
    session_ticket.create_ticket(session_id=SESSION_ID, priority="high", force=True)
    assert stub["events"][0][3] == "high"


def test_the_plan_is_attached_under_its_own_filename(roots, stub):
    out = session_ticket.create_ticket(session_id=SESSION_ID)
    upload = [e for e in stub["events"] if e[0] == "upload"][0]
    assert upload[2] == f"{SLUG}.md"
    assert upload[3] == len(PLAN.encode("utf-8"))
    assert out["attached"] == f"{SLUG}.md"


def test_a_second_run_refuses_rather_than_filing_a_duplicate(roots, stub):
    stub["state"]["existing"] = "File a session as a ticket"
    out = session_ticket.create_ticket(session_id=SESSION_ID)
    assert "already exists" in out["error"]
    assert _kinds(stub) == ["read"]


def test_force_files_it_anyway(roots, stub):
    stub["state"]["existing"] = "File a session as a ticket"
    out = session_ticket.create_ticket(session_id=SESSION_ID, force=True)
    assert out["created"] is True
    assert "read" not in _kinds(stub)


def test_an_unreachable_clickup_is_not_read_as_no_such_task(roots, stub, monkeypatch):
    """read_clickup_task reports 'not found' as an error, so a missing Task and
    a dead network arrive in the same shape. Guessing wrong files a duplicate."""
    def _dead(title, api_key=None):
        stub["events"].append(("read", title))
        return {"error": "503 Server Error"}

    monkeypatch.setattr(clickup, "read_clickup_task", _dead)
    out = session_ticket.create_ticket(session_id=SESSION_ID)
    assert "could not check for an existing task" in out["error"]
    assert _kinds(stub) == ["read"]


def test_a_failed_create_attaches_and_moves_nothing(roots, stub):
    stub["state"]["add_error"] = "403 Forbidden"
    out = session_ticket.create_ticket(session_id=SESSION_ID)
    assert "error" in out
    assert _kinds(stub) == ["read", "add"]


def test_a_failed_attach_still_reports_the_task_that_exists(roots, stub):
    """The Task is already filed by then. An error result would read as though
    nothing had happened, and the user would file it again."""
    stub["state"]["upload_error"] = "413 Payload Too Large"
    out = session_ticket.create_ticket(session_id=SESSION_ID)
    assert out["created"] is True
    assert out["url"] == "https://app.clickup.com/t/abc123"
    assert out["attached"] == ""
    assert any("could not be attached" in w for w in out["warnings"])
    assert "move" in _kinds(stub)


def test_a_failed_move_still_reports_the_task_that_exists(roots, stub):
    stub["state"]["move_error"] = "no status 'designed' in that space"
    out = session_ticket.create_ticket(session_id=SESSION_ID)
    assert out["created"] is True
    assert any("could not be moved" in w for w in out["warnings"])


def test_a_session_with_no_plan_is_refused_before_anything_is_written(roots, stub):
    """A 'designed' Task with no plan on it is exactly what wren-build refuses
    later, so it is refused here instead."""
    (roots["plans"] / f"{SLUG}.md").unlink()
    out = session_ticket.create_ticket(session_id=SESSION_ID)
    assert "no plan file" in out["error"]
    assert stub["events"] == []


def test_a_plan_with_no_heading_is_refused(roots, stub):
    (roots["plans"] / f"{SLUG}.md").write_text("no heading here\n", encoding="utf-8")
    out = session_ticket.create_ticket(session_id=SESSION_ID)
    assert "no '# ' heading" in out["error"]
    assert stub["events"] == []
