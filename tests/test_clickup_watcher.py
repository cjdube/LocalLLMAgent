"""Tests for tasks.clickup_watcher — the poller that turns a ClickUp tag into a
background job.

Nothing here touches the network, ClickUp or the job store: every collaborator
is monkeypatched, and conftest redirects the watcher's state file to tmp_path.
The four properties worth breaking a build over, each asserted on its own:

1. It never calls the model. (test_the_poll_never_calls_the_model)
2. The tag comes off BEFORE the job is queued, and a failed removal queues
   nothing. (the ordering tests)
3. A dead ClickUp is one push, not one per poll. (the failure-counter tests)
4. The comment the job will write cannot reach ClickUp without a tap.
   (test_a_clickup_job_gates_every_write / test_a_clickup_job_may_comment)
"""

import logging

import pytest

from agent import toolset
from tasks import clickup_watcher
from tasks.clickup_watcher import WATCHED_TAGS


@pytest.fixture
def stub(monkeypatch):
    """Stand in for ClickUp and the job store, recording the order of calls —
    the ordering IS the guarantee here, so a stub that only recorded counts
    would let the dangerous order pass."""
    events = []
    prefixes = []
    state = {"tasks": [], "remove_fails": False, "queue_fails": False}

    def _tagged(tags, api_key=None):
        events.append(("fetch", list(tags)))
        if state.get("fetch_error"):
            return {"error": state["fetch_error"]}
        return {"tasks": state["tasks"]}

    def _remove(task_id, tag, api_key=None):
        events.append(("remove", task_id, tag))
        if state["remove_fails"]:
            return {"error": "403 Forbidden"}
        return {"removed": tag, "task_id": task_id}

    def _start(task, origin="chat", comment_prefix=None):
        events.append(("queue", origin, task))
        prefixes.append(comment_prefix)
        if state["queue_fails"]:
            return {"error": "store is full"}
        return {"id": "job1"}

    monkeypatch.setattr(clickup_watcher.clickup, "tagged_clickup_tasks", _tagged)
    monkeypatch.setattr(clickup_watcher.clickup, "remove_clickup_tag", _remove)
    monkeypatch.setattr(clickup_watcher.background, "start_job", _start)

    # setup_logger builds a logger with propagate=False (a task's output stays
    # in its own file), which caplog cannot see. Swap in a propagating one: the
    # WARNING on a failed removal and the ERROR on a dropped request are part of
    # this module's contract, because both are otherwise invisible.
    def _logger(task_name):
        logger = logging.getLogger(f"test_{task_name}")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        logger.propagate = True
        return logger

    monkeypatch.setattr(clickup_watcher, "setup_logger", _logger)
    return type("Stub", (), {"events": events, "state": state,
                             "prefixes": prefixes})()


def _tagged_task(title="Compare the two SDKs", tag="wren-research",
                 task_id="86bbnfav7", description=""):
    return {"id": task_id, "title": title, "watched": [tag],
            "description": description, "space": "Wren", "list": "Backlog",
            "status": "idea", "tags": [tag]}


# --------------------------------------------------------------------------- #
# The prompt Python writes
# --------------------------------------------------------------------------- #

def test_the_job_prompt_names_the_task_by_title_never_by_id():
    """comment_on_clickup_task takes a title, and a small model asked to carry
    an opaque id across a conversation drops it — docs/opaque-identifiers.md."""
    text = clickup_watcher.job_text(_tagged_task(task_id="86bbzzz"), "wren-research")
    assert "Compare the two SDKs" in text
    assert "86bbzzz" not in text


def test_the_job_prompt_carries_the_task_description():
    text = clickup_watcher.job_text(
        _tagged_task(description="MLX vs llama.cpp on the mini"), "wren-research")
    assert "MLX vs llama.cpp on the mini" in text


def test_an_empty_description_leaves_no_dangling_header():
    text = clickup_watcher.job_text(_tagged_task(description=""), "wren-research")
    assert "What it says" not in text


def test_every_template_tells_the_model_to_call_the_tool_in_the_same_turn():
    """Told to "report back", the model describes the comment and stops:
    nothing written, nothing gated, and the reply reads fine. AGENTS.md."""
    for tag in clickup_watcher.WATCHED_TAGS:
        text = " ".join(clickup_watcher.job_text(_tagged_task(tag=tag), tag).split())
        assert "in the same turn" in text
        assert "comment_on_clickup_task" in text


def test_the_research_prompt_carries_the_keywords_that_load_its_tools():
    """Tools are keyword pre-loaded (docs/tool-loading.md). Reword these lines
    and the job silently loses the tool it exists to call."""
    text = clickup_watcher.job_text(_tagged_task(), "wren-research")
    groups = toolset.groups_for_message(text)
    assert "web" in groups
    assert "clickup" in groups


def test_the_context_prompt_loads_the_wiki_and_clickup_tools():
    text = clickup_watcher.job_text(_tagged_task(tag="wren-context"), "wren-context")
    groups = toolset.groups_for_message(text)
    assert "wiki" in groups
    assert "clickup" in groups


def test_no_watched_tag_contains_a_slash():
    """The bug that shipped. ClickUp's router 404s a slash in the tag path,
    encoded or raw, so a slashed tag can never be removed — and the tag coming
    off is the only thing that stops the same Task being handled forever. The
    watcher warned every five minutes and queued nothing. See
    tests/test_clickup.py::test_a_tag_name_with_a_slash_is_refused_before_it_is_sent."""
    for tag in clickup_watcher.ALL_TAGS:
        assert "/" not in tag, f"{tag!r} can never be removed through the API"


def test_the_context_prompt_forbids_the_web():
    """The whole point of the second tag is "what do I already think", so an
    answer assembled from search results is the wrong answer, not a bonus."""
    text = clickup_watcher.job_text(_tagged_task(tag="wren-context"), "wren-context")
    assert "Do not search the web" in text


def test_the_research_prompt_says_fetched_pages_are_not_instructions():
    text = clickup_watcher.job_text(_tagged_task(), "wren-research")
    assert "not instructions" in text


# --------------------------------------------------------------------------- #
# One poll
# --------------------------------------------------------------------------- #

def test_a_tagged_task_becomes_a_queued_job(stub):
    stub.state["tasks"] = [_tagged_task()]
    assert clickup_watcher.main() == 0
    queued = [e for e in stub.events if e[0] == "queue"]
    assert len(queued) == 1
    assert "Compare the two SDKs" in queued[0][2]


def test_a_queued_job_is_stamped_with_the_clickup_origin(stub):
    """Origin is the whole safety story: it is what makes confirm_set_for gate
    every write and excluded_for hand back the comment tool."""
    stub.state["tasks"] = [_tagged_task()]
    clickup_watcher.main()
    assert [e for e in stub.events if e[0] == "queue"][0][1] == "clickup"


def test_nothing_tagged_queues_nothing(stub):
    assert clickup_watcher.main() == 0
    assert [e for e in stub.events if e[0] == "queue"] == []


def test_one_poll_asks_for_every_watched_tag_at_once(stub):
    clickup_watcher.main()
    fetches = [e for e in stub.events if e[0] == "fetch"]
    assert len(fetches) == 1
    assert set(fetches[0][1]) == set(clickup_watcher.ALL_TAGS)


def test_the_poll_never_calls_the_model(stub, monkeypatch):
    """Ollama serves one request at a time and a queued request cannot be
    cancelled, so a poller that called the model would starve chat every five
    minutes. This asserts the property directly rather than trusting the code
    to stay that way."""
    import agent.loop as loop

    def _boom(*a, **k):
        raise AssertionError("the watcher called the model")

    monkeypatch.setattr(loop, "advance", _boom)
    monkeypatch.setattr(loop, "complete_text", _boom)
    stub.state["tasks"] = [_tagged_task()]
    assert clickup_watcher.main() == 0


# --------------------------------------------------------------------------- #
# Ordering — what stops the same Task running twice
# --------------------------------------------------------------------------- #

def test_the_tag_comes_off_before_the_job_is_queued(stub):
    stub.state["tasks"] = [_tagged_task()]
    clickup_watcher.main()
    order = [e[0] for e in stub.events if e[0] in ("remove", "queue")]
    assert order == ["remove", "queue"], "queueing first can re-queue forever"


def test_a_failed_tag_removal_queues_nothing(stub):
    """The dangerous case: tag still on, job queued, so every poll from now on
    queues the same Task again and each one takes the model slot."""
    stub.state["tasks"] = [_tagged_task()]
    stub.state["remove_fails"] = True
    assert clickup_watcher.main() == 0
    assert [e for e in stub.events if e[0] == "queue"] == []


def test_a_task_wearing_both_tags_gets_one_job_this_poll(stub):
    task = _tagged_task()
    task["watched"] = ["wren-research", "wren-context"]
    stub.state["tasks"] = [task]
    clickup_watcher.main()
    assert len([e for e in stub.events if e[0] == "queue"]) == 1
    assert [e for e in stub.events if e[0] == "remove"][0][2] == "wren-research"


def test_a_dropped_request_is_logged_as_an_error(stub, caplog):
    """Tag gone, job not queued: the only other symptom is a comment that never
    arrives, which looks exactly like Wren having nothing to say."""
    stub.state["tasks"] = [_tagged_task()]
    stub.state["queue_fails"] = True
    with caplog.at_level("ERROR"):
        assert clickup_watcher.main() == 0
    assert any("dropped" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# A dead network
# --------------------------------------------------------------------------- #

def test_one_failed_poll_pushes_nothing(stub, monkeypatch):
    pushes = []
    monkeypatch.setattr(clickup_watcher, "notify_failure",
                        lambda *a, **k: pushes.append(a))
    stub.state["fetch_error"] = "connection refused"
    assert clickup_watcher.main() == 0
    assert pushes == []


def test_a_real_outage_pushes_exactly_once(stub, monkeypatch):
    """notify_failure does not dedupe. At a five-minute interval, pushing per
    poll would be ~288 phone alerts a day for one dead router."""
    pushes = []
    monkeypatch.setattr(clickup_watcher, "notify_failure",
                        lambda *a, **k: pushes.append(a))
    stub.state["fetch_error"] = "connection refused"
    for _ in range(clickup_watcher.ALERT_AFTER_FAILURES * 3):
        clickup_watcher.main()
    assert len(pushes) == 1


def test_the_failure_count_resets_once_clickup_answers(stub, monkeypatch):
    pushes = []
    monkeypatch.setattr(clickup_watcher, "notify_failure",
                        lambda *a, **k: pushes.append(a))
    stub.state["fetch_error"] = "connection refused"
    for _ in range(clickup_watcher.ALERT_AFTER_FAILURES - 1):
        clickup_watcher.main()
    stub.state["fetch_error"] = None
    clickup_watcher.main()
    stub.state["fetch_error"] = "connection refused"
    for _ in range(clickup_watcher.ALERT_AFTER_FAILURES - 1):
        clickup_watcher.main()
    assert pushes == [], "the counter did not reset, so a blip still alerts"


def test_an_unreachable_clickup_is_not_a_failed_launchd_job(stub):
    stub.state["fetch_error"] = "connection refused"
    assert clickup_watcher.main() == 0


# --------------------------------------------------------------------------- #
# The policy the origin buys. These live here because this module is the only
# thing that creates a "clickup" job — if the policy moves, this is what breaks.
# --------------------------------------------------------------------------- #

def test_a_clickup_job_may_comment():
    """The comment is the entire job. Excluded, the model would not have the
    tool and the tagged Task would silently never get an answer."""
    assert "comment_on_clickup_task" not in toolset.excluded_for("clickup")


def test_a_clickup_job_still_cannot_create_tasks():
    """A tagged Task asks for an answer on itself. A job that can create Tasks
    can grow the workspace while nobody is looking."""
    assert "add_clickup_task" in toolset.excluded_for("clickup")


def test_a_clickup_job_still_cannot_write_memories_or_skills():
    excluded = toolset.excluded_for("clickup")
    for tool in ("remember", "pin", "write_skill", "run_in_background"):
        assert tool in excluded


def test_a_clickup_job_gates_every_write(stub):
    """The user tagged and walked away, so nothing he has not read may be
    written. In practice this is the single comment, shown on his phone."""
    gated = toolset.confirm_set_for("clickup")
    assert toolset.WRITE_TOOLS <= gated
    assert "comment_on_clickup_task" in gated


def test_the_clickup_origin_did_not_loosen_the_other_two():
    assert "comment_on_clickup_task" in toolset.excluded_for("mail")
    assert "comment_on_clickup_task" in toolset.excluded_for("chat")
    assert toolset.confirm_set_for("chat") == toolset.CONSEQUENTIAL_TOOLS


# --------------------------------------------------------------------------- #
# The comment prefix. The comment lands under Craig's own name (it is his
# token) and the tag that asked for it is gone by then, so without this he
# cannot tell his own note from Wren's answer.
# --------------------------------------------------------------------------- #

def test_the_prefix_is_the_tag_that_was_removed():
    assert clickup_watcher.comment_prefix("wren-research") == "wren-research:"
    assert clickup_watcher.comment_prefix("wren-context") == "wren-context:"


def test_every_watched_tag_can_be_a_prefix():
    for tag in WATCHED_TAGS:
        assert clickup_watcher.comment_prefix(tag).startswith(tag)


def test_the_job_carries_the_prefix_so_python_can_enforce_it(stub):
    """Asked for in the prompt AND stamped on in Python. The prompt half is so
    the approval card shows the line he will actually see; the Python half is
    what makes it true when the model leaves it out."""
    stub.state["tasks"] = [_tagged_task()]
    clickup_watcher.main()
    assert stub.prefixes == ["wren-research:"]


def test_every_template_asks_the_model_for_the_prefix_too():
    for tag in WATCHED_TAGS:
        text = " ".join(clickup_watcher.job_text(_tagged_task(tag=tag), tag).split())
        assert f'STARTS with "{tag}:"' in text


# --------------------------------------------------------------------------- #
# wren-build: the tag that queues a Claude Code run
#
# Three preconditions, and each one must fail on its own AND leave a comment.
# The tag is gone by the time any of them is evaluated, so silence would be
# indistinguishable from a broken watcher — and the user would have no reason
# to look.
# --------------------------------------------------------------------------- #

@pytest.fixture
def build_stub(stub, monkeypatch):
    """ClickUp detail reads, the plan download, the queue and the status move,
    all recorded in one ordered list — the ordering IS the guarantee here."""
    state = {
        "detail": {"id": "86bbnfav7", "title": "Add up-arrow recall",
                   "status": "designed", "description": "",
                   "attachments": [{"id": "a1", "title": "a-plan.md",
                                    "extension": "md", "size": 6071,
                                    "url": "https://x.clickup-attachments.com/a-plan.md"}]},
        "plan": {"text": "## Context\nDo exactly this.\n"},
        "queue": {"id": "job9"},
        "move": {},
    }

    def _detail(task_id, api_key=None):
        stub.events.append(("detail", task_id))
        return state["detail"]

    def _download(url, **kw):
        stub.events.append(("download", url))
        return state["plan"]

    def _enqueue(task_id, title, plan_text, plan_name):
        stub.events.append(("enqueue", title, plan_name, plan_text))
        return state["queue"]

    def _comment(title, comment, api_key=None):
        stub.events.append(("comment", title, comment))
        return {}

    def _move(title, status, api_key=None):
        stub.events.append(("move", title, status))
        return state["move"]

    monkeypatch.setattr(clickup_watcher.clickup, "clickup_task_detail", _detail)
    monkeypatch.setattr(clickup_watcher.clickup, "download_attachment", _download)
    monkeypatch.setattr(clickup_watcher.clickup, "comment_on_clickup_task", _comment)
    monkeypatch.setattr(clickup_watcher.clickup, "move_clickup_task", _move)
    monkeypatch.setattr(clickup_watcher.build_queue, "enqueue", _enqueue)
    stub.state["tasks"] = [_tagged_task(title="Add up-arrow recall", tag="wren-build")]
    stub.build = state
    return stub


def _kinds(stub):
    return [e[0] for e in stub.events]


def _comments(stub):
    return [e[2] for e in stub.events if e[0] == "comment"]


def test_a_designed_task_with_one_plan_is_queued(build_stub):
    clickup_watcher.main()
    queued = [e for e in build_stub.events if e[0] == "enqueue"]
    assert len(queued) == 1
    assert queued[0][1] == "Add up-arrow recall"
    assert queued[0][2] == "a-plan.md"
    assert "Do exactly this." in queued[0][3]


def test_the_plan_reaches_the_queue_verbatim(build_stub):
    build_stub.build["plan"] = {"text": "## Step 1\nEdit the thing.\n"}
    clickup_watcher.main()
    assert [e for e in build_stub.events if e[0] == "enqueue"][0][3] \
        == "## Step 1\nEdit the thing.\n"


def test_a_queued_build_moves_the_task_to_building(build_stub):
    clickup_watcher.main()
    assert ("move", "Add up-arrow recall", "building") in build_stub.events


def test_the_tag_comes_off_before_the_build_is_queued(build_stub):
    """The standing rule, and it costs more here than anywhere else: queue-first
    plus a failed removal starts a paid Claude Code run on every poll forever."""
    clickup_watcher.main()
    kinds = _kinds(build_stub)
    assert kinds.index("remove") < kinds.index("enqueue")


def test_the_detail_is_read_before_the_tag_comes_off(build_stub):
    """A ClickUp blip on that read must leave the tag on so the next poll
    retries. Once the tag is off, the request is spent."""
    clickup_watcher.main()
    kinds = _kinds(build_stub)
    assert kinds.index("detail") < kinds.index("remove")


def test_an_unreadable_task_keeps_its_tag_and_queues_nothing(build_stub):
    build_stub.build["detail"] = {"error": "HTTP 502"}
    clickup_watcher.main()
    assert "remove" not in _kinds(build_stub)
    assert "enqueue" not in _kinds(build_stub)


def test_a_failed_tag_removal_queues_no_build(build_stub):
    build_stub.state["remove_fails"] = True
    clickup_watcher.main()
    assert "enqueue" not in _kinds(build_stub)


# ---- the three preconditions, one at a time -------------------------------

def test_a_task_that_is_not_designed_is_refused_and_says_so(build_stub):
    build_stub.build["detail"]["status"] = "idea"
    clickup_watcher.main()
    assert "enqueue" not in _kinds(build_stub)
    assert "designed" in _comments(build_stub)[0]


def test_a_task_with_no_plan_is_refused_and_says_so(build_stub):
    build_stub.build["detail"]["attachments"] = []
    clickup_watcher.main()
    assert "enqueue" not in _kinds(build_stub)
    assert "no plan is attached" in _comments(build_stub)[0]


def test_two_plans_are_refused_rather_than_guessed(build_stub):
    """Ambiguity is a question, never a default — the standing posture across
    this module. Picking one would build the wrong plan, and the user would
    have no reason to look."""
    build_stub.build["detail"]["attachments"] = [
        {"title": "old-plan.md", "extension": "md", "url": "https://x/1.md"},
        {"title": "new-plan.md", "extension": "md", "url": "https://x/2.md"},
    ]
    clickup_watcher.main()
    assert "enqueue" not in _kinds(build_stub)
    comment = _comments(build_stub)[0]
    assert "old-plan.md" in comment and "new-plan.md" in comment


def test_a_non_markdown_attachment_is_not_mistaken_for_a_plan(build_stub):
    """A Task may well carry a screenshot as well. Those are not instructions."""
    build_stub.build["detail"]["attachments"] = [
        {"title": "screenshot.png", "extension": "png", "url": "https://x/1.png"}]
    clickup_watcher.main()
    assert "enqueue" not in _kinds(build_stub)
    assert "no plan is attached" in _comments(build_stub)[0]


def test_every_refusal_says_how_to_restart_it(build_stub):
    """The tag is gone, so the comment is the only thing that tells him a
    re-tag is what starts it again."""
    for attachments in ([], [{"title": "a.md", "extension": "md", "url": "https://x/a"},
                             {"title": "b.md", "extension": "md", "url": "https://x/b"}]):
        build_stub.events.clear()
        build_stub.build["detail"]["attachments"] = attachments
        clickup_watcher.main()
        assert "re-apply the tag" in _comments(build_stub)[0]


def test_a_refusal_is_signed_with_the_tag_that_asked_for_it(build_stub):
    """The comment lands under the user's own name — it is his token — and the
    tag is off by then, so without the prefix he cannot tell his own note from
    Wren's."""
    build_stub.build["detail"]["status"] = "idea"
    clickup_watcher.main()
    assert _comments(build_stub)[0].startswith("wren-build:")


# ---- everything after the tag is off still reports -------------------------

def test_an_undownloadable_plan_is_reported_not_swallowed(build_stub):
    build_stub.build["plan"] = {"error": "HTTP 404"}
    clickup_watcher.main()
    assert "enqueue" not in _kinds(build_stub)
    assert "HTTP 404" in _comments(build_stub)[0]


def test_a_full_queue_is_reported_on_the_task(build_stub):
    build_stub.build["queue"] = {"error": "build queue is full (5 waiting)"}
    clickup_watcher.main()
    assert "full" in _comments(build_stub)[0]
    assert ("move", "Add up-arrow recall", "building") not in build_stub.events


def test_a_failed_status_move_never_cancels_a_queued_build(build_stub):
    """The move is cosmetic; the build is the thing. Losing the build because
    the board could not be updated would be the wrong trade."""
    build_stub.build["move"] = {"error": "HTTP 500"}
    clickup_watcher.main()
    assert len([e for e in build_stub.events if e[0] == "enqueue"]) == 1


def test_the_build_path_never_queues_a_model_job(build_stub):
    """Claude Code does the thinking. Ollama serves one request at a time, and a
    build routed through bg_worker would take that slot for tens of minutes."""
    clickup_watcher.main()
    assert "queue" not in _kinds(build_stub)
