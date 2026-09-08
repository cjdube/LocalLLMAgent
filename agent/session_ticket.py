"""File a Claude Code session as a ClickUp Task: the plan's heading becomes the
title, the first prompt and the reply it drew become quotes in the description,
the plan .md is attached, and the Task lands in `designed`.

The caller is the `clickup-ticket` skill (~/.claude/skills/clickup-ticket),
which knows which session it is in and shells out to this module's CLI. The
logic lives here rather than in the skill so pytest covers it — a skill file is
prose, and prose cannot be run.

**Nothing here calls the model.** The title is read off the plan, the quote is
the user's own words, and the date comes from the transcript. A model asked to
re-type any of it can only get it wrong (docs/model-constraints.md).

**Two anchors reach the same transcript**, and either one is enough:
  --session <uuid>  the session id, which is also the name of Claude Code's
                    scratchpad folder, so the skill always has it
  --plan <path>     the plan file, whose basename IS the session's `slug` field

**Create, attach, then move.** The move to `designed` is last so the status
never claims a Task is designed while its plan is still missing. Each step
after the create can fail on its own, and the result says which did — a Task
that exists but has no plan on it must not read as "nothing happened".

Usage:
    python -m agent.session_ticket --session <uuid> [--priority high] [--dry-run]
    python -m agent.session_ticket --plan ~/.claude/plans/some-plan.md
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.dates import local_timezone
from agent.tools import clickup
from agent.tools._http import print_result

# Where Claude Code keeps transcripts and plans. Overridable only so the tests
# can point them at a tmp_path — there is no reason to move them in real use.
_PROJECTS_ROOT = "~/.claude/projects"
_PLANS_ROOT = "~/.claude/plans"

# A prompt that opens with one of these is not something the user typed: it is
# a slash-command wrapper, a hook's stdout, or context the harness injected.
# The origin check below already rejects most of them; this catches the rest.
_INJECTED_PREFIXES = (
    "<command-name", "<local-command", "<system-reminder",
    "<bash-input", "<scheduled-task",
)

# ClickUp truncates a description over its own limit without saying so, so the
# quotes are cut here instead, with a line that says how much was dropped.
# _MAX_NEW_DESCRIPTION_CHARS is 4000, and these two plus their labels and the
# provenance line have to fit inside it with the "> " on every line.
#
# The ask gets the larger share of the room. The reply is context — what the
# session set off to do — and its first paragraph carries almost all of that,
# while the ask is the thing the Task is actually about.
_QUOTE_BUDGET = 2200
_REPLY_BUDGET = 1000

# Reading a whole transcript to find one prompt and one title is cheap, but a
# 13 MB session exists on this machine. Streaming keeps that bounded; this only
# stops a pathological file from being read at all.
_MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024


def _projects_root() -> Path:
    return Path(os.getenv("WREN_CLAUDE_PROJECTS_ROOT", _PROJECTS_ROOT)).expanduser()


def _plans_root() -> Path:
    return Path(os.getenv("WREN_CLAUDE_PLANS_ROOT", _PLANS_ROOT)).expanduser()


def _human_text(rec: dict) -> str | None:
    """The text of one transcript line if a person typed it, else None.

    `origin.kind == "human"` is the field that separates a submitted prompt
    from everything else on a `type: "user"` line — tool results, hook output,
    slash-command wrappers and injected reminders all arrive as `user` lines
    too, and the first one in a file is very often not a prompt at all.
    `promptSource == "typed"` is the same fact on older terminal sessions.

    `message.content` is a plain string for a typed prompt but an ARRAY when
    the prompt carried an image, and that array holds a megabyte of base64. So
    only text blocks are read out of it, never the whole thing.
    """
    if rec.get("isMeta") is True:
        return None
    if rec.get("toolUseResult") is not None:
        return None
    origin = rec.get("origin") or {}
    if origin.get("kind") != "human" and rec.get("promptSource") != "typed":
        return None

    content = ((rec.get("message") or {}).get("content"))
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        return None

    text = text.strip()
    if not text or text.startswith(_INJECTED_PREFIXES):
        return None
    return text


def _assistant_text(rec: dict) -> str:
    """The prose out of one assistant line, and nothing else.

    An assistant line holds exactly ONE content block, and one API response is
    split across several lines that share a `requestId` — thinking, then text,
    then a tool_use per call. Only the text blocks are the reply; the thinking
    is not shown to the user and a tool_use is a JSON payload.
    """
    content = ((rec.get("message") or {}).get("content"))
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    return "\n".join(
        b.get("text", "") for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    ).strip()


def _read_transcript(path: Path) -> dict:
    """Stream one .jsonl session and pull out the four facts we need.

    Line by line, with a cheap string test before json.loads: these files run to
    megabytes and most lines are assistant output we do not want.

    The title lines are appended repeatedly as the title is re-asserted, so the
    LAST one wins, and a title the user set by hand beats the generated one.
    They also carry only `type` and `sessionId` — no uuid, no timestamp — so
    nothing here may assume a line is shaped like a message.
    """
    slug = ai_title = custom_title = first_prompt = started_at = None
    reply_request, reply_parts = None, []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"type"' not in line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(rec, dict):
                continue

            kind = rec.get("type")
            if kind == "custom-title":
                custom_title = rec.get("customTitle") or custom_title
            elif kind == "ai-title":
                ai_title = rec.get("aiTitle") or ai_title
            if slug is None and rec.get("slug"):
                slug = rec["slug"]
            if first_prompt is None and kind == "user":
                text = _human_text(rec)
                if text:
                    first_prompt = text
                    started_at = rec.get("timestamp")
            elif first_prompt and kind == "assistant" and not rec.get("isSidechain"):
                # The first answer to that prompt, gathered across the lines
                # that share its requestId. A line with no text — thinking, or
                # a tool call — is skipped without ending the reply, because
                # the text block often sits between two of them. A DIFFERENT
                # requestId is a later turn, and is what keeps the rest of the
                # session out.
                text = _assistant_text(rec)
                if not text:
                    continue
                if reply_request is None:
                    reply_request = rec.get("requestId")
                if rec.get("requestId") == reply_request:
                    reply_parts.append(text)

    return {
        "slug": slug,
        "session_title": custom_title or ai_title or "",
        "first_prompt": first_prompt,
        "first_reply": "\n\n".join(reply_parts),
        "started_at": started_at,
    }


def _local_stamp(iso_utc: str | None) -> str:
    """A transcript timestamp is UTC with a Z; the day we print is the local
    one. Converted, never sliced — docs/timezones.md."""
    if not iso_utc:
        return ""
    try:
        moment = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return ""
    return moment.astimezone(ZoneInfo(local_timezone())).strftime("%Y-%m-%d %H:%M")


def _transcript_for_session(session_id: str) -> Path | None:
    """<projects>/<mangled cwd>/<session id>.jsonl. The project folder is the
    working directory with its separators mangled, and reversing that is lossy,
    so the id is globbed across all of them instead."""
    for found in sorted(_projects_root().glob(f"*/{session_id}.jsonl")):
        return found
    return None


def _transcript_for_slug(slug: str) -> tuple:
    """The one transcript carrying this slug, or (None, why).

    Every line of a session carries the same `slug`, and a slug belongs to
    exactly one session (checked against all 338 transcripts on this machine),
    so a plan file identifies its session with no other index needed.
    Subagent transcripts live one folder deeper, so */*.jsonl skips them.
    """
    hits = []
    for path in sorted(_projects_root().glob("*/*.jsonl")):
        try:
            if path.stat().st_size > _MAX_TRANSCRIPT_BYTES:
                continue
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    # Substring first because it is cheap over megabytes, then
                    # parsed to confirm: the raw text also matches a line that
                    # merely quotes the slug, and matching on the exact
                    # '"slug":"…"' bytes would instead depend on the writer's
                    # JSON spacing.
                    if slug not in line:
                        continue
                    try:
                        if json.loads(line).get("slug") == slug:
                            hits.append(path)
                            break
                    except (ValueError, TypeError, AttributeError):
                        continue
        except OSError:
            continue
    if not hits:
        return None, f"no Claude Code session carries the slug '{slug}'"
    if len(hits) > 1:
        return None, (f"the slug '{slug}' matches {len(hits)} sessions — "
                      "pass --session <id> instead")
    return hits[0], None


def _plan_for_slug(slug: str) -> Path | None:
    """<plans>/<slug>.md, if it is there.

    The slug is text read out of a file, so it is not trusted to build a path:
    the result is resolved and checked to be inside the plans folder before it
    is opened. Subagent plans are a <slug>-agent-<hex>.md variant and are not
    this session's plan.
    """
    root = _plans_root().resolve()
    candidate = (root / f"{slug}.md").resolve()
    if root not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def _plan_heading(plan_text: str) -> str:
    """The plan's H1, cleaned up enough to be a Task title. Backticks are
    Markdown decoration and would show up literally on the board."""
    for line in plan_text.splitlines():
        if line.startswith("# "):
            return line[2:].replace("`", "").strip()
    return ""


def session_facts(session_id: str = None, plan_path: str = None) -> dict:
    """Everything the ticket is built from, for one session. Either anchor."""
    if not session_id and not plan_path:
        return {"error": "pass --session <id> or --plan <path>"}

    given_plan = None
    if plan_path:
        given_plan = Path(plan_path).expanduser().resolve()
        if not given_plan.is_file():
            return {"error": f"no plan file at {given_plan}"}

    if session_id:
        transcript = _transcript_for_session(session_id)
        if transcript is None:
            return {"error": f"no Claude Code transcript for session {session_id}"}
    else:
        transcript, why = _transcript_for_slug(given_plan.stem)
        if why:
            return {"error": why}

    facts = _read_transcript(transcript)
    if not facts["first_prompt"]:
        return {"error": f"no typed prompt found in {transcript.name} — "
                         "this session has nothing to quote"}
    if not facts["slug"]:
        return {"error": f"{transcript.name} carries no slug, so its plan cannot be found"}

    plan = given_plan or _plan_for_slug(facts["slug"])
    facts.update({
        "session_id": transcript.stem,
        "transcript": str(transcript),
        "started_local": _local_stamp(facts.pop("started_at")),
        "plan_path": str(plan) if plan else "",
        "plan_h1": _plan_heading(plan.read_text(encoding="utf-8")) if plan else "",
    })
    return facts


def quote_block(first_prompt: str, budget: int = _QUOTE_BUDGET) -> str:
    """The prompt as a Markdown blockquote, cut to a budget **out loud**.

    ClickUp silently truncates an over-long description, which would leave a
    quote ending mid-word with nothing to say it had been cut. Cutting here
    means the Task can say so.
    """
    text = (first_prompt or "").strip()
    dropped = 0
    if len(text) > budget:
        cut = text[:budget].rsplit(None, 1)[0]
        dropped = len(text) - len(cut)
        text = cut
    lines = [f"> {line}" if line.strip() else ">" for line in text.splitlines()]
    if dropped:
        lines.append(">")
        lines.append(f"> _[cut here — {dropped} more characters in the original prompt]_")
    return "\n".join(lines)


def description_for(facts: dict) -> str:
    """The whole Task description, written in Python: what was asked, what came
    back, then one line saying where both came from.

    The reply is included because the ask on its own is often a question, and
    the answer to it is what the Task is really for. It is omitted entirely
    when the session has none — a heading over an empty quote reads like a bug.
    """
    parts = ["**Asked**", "", quote_block(facts["first_prompt"])]
    reply = (facts.get("first_reply") or "").strip()
    if reply:
        parts += ["", "**Answered**", "", quote_block(reply, _REPLY_BUDGET)]

    where = f"_From Claude Code session `{facts['slug']}`"
    if facts.get("started_local"):
        where += f", {facts['started_local']}"
    where += "._"
    parts += ["", where]
    return "\n".join(parts)


def _already_filed(title: str) -> dict:
    """Whether a Task of this title exists already. Returns {} when it does not.

    read_clickup_task reports "not found" as an error, so a missing Task and an
    unreachable ClickUp arrive in the same shape. They are told apart on the
    message, because guessing the wrong way files a duplicate.
    """
    found = clickup.read_clickup_task(title)
    if "error" not in found:
        return {"error": f"a ClickUp task called '{found.get('title', title)}' already "
                         "exists. Pass --force to file another one.",
                "url": found.get("url", "")}
    if "no ClickUp task matching" in found["error"]:
        return {}
    return {"error": f"could not check for an existing task: {found['error']}"}


def create_ticket(session_id: str = None, plan_path: str = None,
                  priority: str = "normal", space: str = "Wren",
                  status: str = "designed", dry_run: bool = False,
                  force: bool = False) -> dict:
    """File the session as a Task. Create, attach, then move."""
    facts = session_facts(session_id, plan_path)
    if "error" in facts:
        return facts
    if not facts["plan_path"]:
        return {"error": f"no plan file at {_plans_root()}/{facts['slug']}.md — "
                         "write the plan first, or pass --plan <path>"}
    title = facts["plan_h1"]
    if not title:
        return {"error": f"{Path(facts['plan_path']).name} has no '# ' heading, "
                         "so there is no title to file it under"}

    plan_file = Path(facts["plan_path"])
    description = description_for(facts)
    preview = {
        "title": title,
        "space": space,
        "priority": priority,
        "status": status,
        "plan": plan_file.name,
        "session": facts["session_id"],
        "description": description,
    }
    if dry_run:
        return {"dry_run": True, **preview}

    if not force:
        clash = _already_filed(title)
        if clash:
            return clash

    created = clickup.add_clickup_task(title=title, space=space,
                                       description=description, priority=priority)
    if "error" in created:
        return created

    out = {
        "created": True,
        "title": created.get("title", title),
        "space": created.get("space", space),
        "list": created.get("list", ""),
        "priority": priority,
        "url": created.get("url", ""),
        "status": created.get("status", ""),
        "session": facts["session_id"],
        "plan": plan_file.name,
        "attached": "",
        "warnings": [],
    }

    # Everything from here can fail on its own, and the Task already exists.
    # Each failure is a warning on a successful result, not an error that would
    # read as though nothing had been filed.
    found = clickup.find_task_id(title)
    if "error" in found:
        out["warnings"].append(f"filed, but the plan could not be attached: {found['error']}")
        return out

    got = clickup.upload_attachment(found["id"], plan_file.name,
                                    plan_file.read_bytes())
    if "error" in got:
        out["warnings"].append(f"filed, but the plan could not be attached: {got['error']}")
    else:
        out["attached"] = got.get("attached", plan_file.name)

    moved = clickup.move_clickup_task(title=title, status=status)
    if "error" in moved:
        out["warnings"].append(f"filed, but it could not be moved to '{status}': "
                               f"{moved['error']}")
    else:
        out["status"] = moved.get("status", status)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="File a Claude Code session as a ClickUp Task.")
    parser.add_argument("--session", default=None, help="session id (the scratchpad folder name)")
    parser.add_argument("--plan", default=None, help="path to the plan .md, as an alternative anchor")
    parser.add_argument("--priority", default="normal", choices=["urgent", "high", "normal", "low"])
    parser.add_argument("--space", default="Wren")
    parser.add_argument("--status", default="designed")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="print what would be filed and write nothing")
    parser.add_argument("--force", action="store_true",
                        help="file it even though a Task of the same title exists")
    args = parser.parse_args()

    return print_result(create_ticket(
        session_id=args.session, plan_path=args.plan, priority=args.priority,
        space=args.space, status=args.status, dry_run=args.dry_run, force=args.force,
    ))


if __name__ == "__main__":
    sys.exit(main())
