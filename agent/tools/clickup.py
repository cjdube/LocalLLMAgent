"""ClickUp — read and write Spaces, Lists and Tasks.

ClickUp's own three nouns, used exactly as ClickUp uses them: a **Space** is a
top-level area of the workspace, a **List** sits inside a Space, and a **Task**
sits on a List. Nothing here assumes any of them is called anything in
particular; every name is discovered from the account on the call that needs it.

Three reads — list_clickup_spaces (what Spaces and Lists exist, and the
statuses each Space defines), list_clickup_tasks (Tasks, filterable by Space,
List and status) and read_clickup_task (one Task in full, with its description
and comments) — and three writes: add_clickup_task, move_clickup_task and
comment_on_clickup_task. Every write is in WRITE_TOOLS, so it pauses for a tap
on the phone before it runs, and the two that write free text are barred from
unattended runs (see the Writes section).

Spaces and Lists are addressed by a slug of their NAME, never by id: the model
is repeating back a word the user said out loud, not an identifier it was
handed (docs/opaque-identifiers.md). They are discovered on every call rather
than pinned in config — one extra GET, and adding or renaming a Space or List
needs no edit here and no restart.

Statuses are per-Space in ClickUp and genuinely differ between them, so a
status is always validated against the Space it is being used in, and the
error names the ones that Space actually defines.

Ambiguity is a question, never a default. An unnamed List in a Space holding
several, a title matching two Tasks, a token seeing two workspaces: each one
refuses and names the options rather than picking the first.

Usage:
    python -m agent.tools.clickup spaces
    python -m agent.tools.clickup tasks [--space wren] [--list Backlog]
                                        [--status parked] [--include-done]
    python -m agent.tools.clickup read --title "starred releases"
    python -m agent.tools.clickup digest [--since-days 1]
    python -m agent.tools.clickup add --title "..." --space wren [--list Backlog]
                                      [--description "..."]
    python -m agent.tools.clickup move --title "..." --status parked
    python -m agent.tools.clickup comment --title "..." --comment "..."

Key resolution order: --api-key arg > config/.env file > CLICKUP_API_TOKEN env var
"""

import argparse
import re
import sys
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

from agent import prefs
from agent.dates import local_timezone
from agent.tools._http import http_error, load_env, missing_key_error, print_result, resolve_key

load_env()

# The user's name, for the model-facing tool descriptions below. From
# config/preferences.json; falls back to "the user".
_NAME = prefs.user_name()

API_ROOT = "https://api.clickup.com/api/v2"

# ClickUp's personal token is sent raw, with no "Bearer" prefix — a Bearer
# header is what an OAuth app token uses, and mixing them up returns 401.
TIMEOUT_S = 15

# 100 tasks per page. The ceiling bounds the walk so an unexpectedly large
# workspace can't spin the loop; it is far above any personal backlog and, like
# google_tasks._MAX_FETCH, bounds the FETCH rather than the reported count.
_PAGE_SIZE = 100
_MAX_PAGES = 10

# And the budget that actually bounds the reply. A backlog row is ~120 chars
# and this account already holds 57 items across three spaces — 6800 chars
# against the loop's 8000 default, before a single new idea is captured. A row
# cap would not have caught that: it bounds how many rows come back, never how
# big they are (docs/row-caps-need-char-budgets is the standing version of this).
_MAX_LIST_CHARS = 6000

# read_clickup_task returns one item the user asked for, so it gets more room
# than a listing — but a description runs to over a thousand characters already
# and comments are unbounded, so both are trimmed here rather than left to the
# loop's backstop, which would slice mid-sentence and say nothing about it.
_MAX_DESCRIPTION_CHARS = 2000
_MAX_COMMENTS = 10
_MAX_COMMENT_CHARS = 2500


class _ClickUpError(Exception):
    """A configuration- or lookup-shaped failure with a message meant to be
    read: no workspace, an unknown space, an unknown status. Distinct from a
    requests failure, which http_error already renders."""


def _slug(text: str) -> str:
    """Lowercase, strip everything but letters and digits. 'Vibe Foundry' and
    'vibe-foundry' both become 'vibefoundry', because the model is passing back
    a name the user said out loud, not an identifier it was handed."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _get(path: str, token: str, **params) -> dict:
    resp = requests.get(
        f"{API_ROOT}{path}",
        headers={"Authorization": token},
        params=params or None,
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


def _write(method: str, path: str, token: str, payload: dict) -> dict:
    """POST/PUT/DELETE with the same raw-token header and explicit timeout as
    _get. Separate from _get so every call site that CHANGES something in
    ClickUp is greppable — these are the only lines in this module that do.
    payload is None for DELETE, which sends no body."""
    resp = requests.request(
        method,
        f"{API_ROOT}{path}",
        headers={"Authorization": token, "Content-Type": "application/json"},
        json=payload,
        timeout=TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def _team_id(token: str) -> str:
    teams = _get("/team", token).get("teams", [])
    if not teams:
        raise _ClickUpError("this ClickUp token has no workspaces")
    if len(teams) > 1:
        names = ", ".join(t.get("name", t["id"]) for t in teams)
        raise _ClickUpError(
            f"this token sees {len(teams)} ClickUp workspaces ({names}); "
            "the tools assume one and would silently pick the first"
        )
    return teams[0]["id"]


def _spaces(token: str, team_id: str) -> list:
    """Every Space on the workspace, as an space: slug, display name, id, and
    the statuses that Space actually defines."""
    spaces = _get(f"/team/{team_id}/space", token, archived="false").get("spaces", [])
    return [
        {
            "space": _slug(s.get("name", "")),
            "name": s.get("name", ""),
            "id": s["id"],
            "statuses": [
                {"status": st.get("status", ""), "type": st.get("type", "")}
                for st in s.get("statuses", [])
            ],
        }
        for s in spaces
        if s.get("id")
    ]


def _resolve_space(spaces: list, space: str) -> list:
    """The one space named, or all of them when nothing was named."""
    if not space or not space.strip():
        return spaces
    wanted = _slug(space)
    match = [a for a in spaces if a["space"] == wanted]
    if not match:
        known = ", ".join(a["space"] for a in spaces)
        raise _ClickUpError(f"no space named '{space}'. Spaces: {known}")
    return match


def _lists(token: str, space_id: str) -> list:
    """Every List in a Space — the ones sitting directly on it and the ones
    inside its Folders, flattened. A Folder is an organising layer in ClickUp,
    not a place Tasks live, so it is not a level this module models.

    Fetched only when a caller actually needs to name a List, because it is a
    GET per Space and the listing path does not need one."""
    lists = _get(f"/space/{space_id}/list", token, archived="false").get("lists", [])
    folders = _get(f"/space/{space_id}/folder", token, archived="false").get("folders", [])
    for f in folders:
        lists.extend(f.get("lists", []))
    return [{"list": _slug(l.get("name", "")), "name": l.get("name", ""), "id": l["id"]}
            for l in lists if l.get("id")]


def _resolve_list(lists: list, name: str, space_name: str) -> dict:
    """The one List named, or the Space's only List when nothing was named.

    A Space with several Lists and no name given is a QUESTION, not a default.
    Picking the first would file a Task somewhere nobody asked for, and nothing
    in the reply would tell the user to go and look — the same posture as
    _team_id with two workspaces and _find_task with two matching titles."""
    if not lists:
        raise _ClickUpError(f"the '{space_name}' Space has no List to write to")
    if name and name.strip():
        wanted = _slug(name)
        match = [l for l in lists if l["list"] == wanted]
        if not match:
            known = ", ".join(l["name"] for l in lists)
            raise _ClickUpError(
                f"no List named '{name}' in the '{space_name}' Space. Lists: {known}"
            )
        return match[0]
    if len(lists) > 1:
        known = ", ".join(l["name"] for l in lists)
        raise _ClickUpError(
            f"the '{space_name}' Space holds {len(lists)} Lists ({known}) — "
            "say which one rather than leaving it to be guessed"
        )
    return lists[0]


def _opening_status(space: dict) -> str:
    """What a newly captured item starts as: the Space's own first Not-Started
    status — 'idea' in the Wren Space, 'to do' in the others. Read off the
    Space rather than passed in, so the model never picks a status on create
    and never has to know one Space's workflow differs from another's."""
    for st in space["statuses"]:
        if st["type"] == "open":
            return st["status"]
    raise _ClickUpError(f"space '{space['space']}' defines no not-started status")


# ClickUp's priority field is INVERTED and numeric: 1 is Urgent, 4 is Low. The
# model says the word; Python owns the number. Never make it emit the digit.
_PRIORITY_TO_API = {"urgent": 1, "high": 2, "normal": 3, "low": 4}


def _resolve_priority(priority: str) -> int:
    n = _PRIORITY_TO_API.get(_slug(priority))
    if n is None:
        raise _ClickUpError(
            f"no priority '{priority}'. Priorities: {', '.join(_PRIORITY_TO_API)}"
        )
    return n


def _resolve_status(chosen: list, status: str) -> str:
    """Validate a status against what the chosen spaces actually define, and
    hand back its canonical spelling. ClickUp statuses are per-Space, so an
    unvalidated filter silently returns nothing rather than saying it was
    given a status that does not exist here."""
    wanted = _slug(status)
    for a in chosen:
        for st in a["statuses"]:
            if _slug(st["status"]) == wanted:
                return st["status"]
    known = sorted({st["status"] for a in chosen for st in a["statuses"]})
    where = chosen[0]["space"] if len(chosen) == 1 else "these spaces"
    raise _ClickUpError(f"no status '{status}' in {where}. Statuses: {', '.join(known)}")


def _fetch_tasks(token: str, team_id: str, space_ids: list, include_done: bool,
                 updated_after_ms: int = None, list_ids: list = None,
                 tags: list = None) -> list:
    """Every task in the given Spaces, paged. ClickUp excludes its Closed
    status group by default and that is not a rounding error here — 21 of this
    account's 57 items are shipped — so include_done is the difference between
    "the backlog" and "everything ever tracked", not a nicety."""
    tasks, page = [], 0
    while page < _MAX_PAGES:
        params = {"page": page, "space_ids[]": space_ids}
        if include_done:
            params["include_closed"] = "true"
        if updated_after_ms is not None:
            params["date_updated_gt"] = int(updated_after_ms)
        if list_ids:
            params["list_ids[]"] = list_ids
        if tags:
            # Verified live against the real workspace: several tags[] values
            # are OR-ed, not AND-ed, so one call covers every watched tag no
            # matter how many there are. Do not "fix" this into a loop.
            params["tags[]"] = tags
        body = _get(f"/team/{team_id}/task", token, **params)
        batch = body.get("tasks", [])
        tasks.extend(batch)
        if body.get("last_page") or len(batch) < _PAGE_SIZE:
            break
        page += 1
    return tasks


def _ms_to_local_date(ms) -> str | None:
    """ClickUp timestamps are Unix milliseconds (sometimes a string, sometimes
    an int) and are UTC; the day we report is the local one. Never slice the
    number or the model's idea of today into this — docs/timezones.md."""
    if ms in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, ZoneInfo(local_timezone())).date().isoformat()
    except (ValueError, TypeError, OSError):
        return None


def _days_since(iso_day: str | None, today: date | None = None) -> int | None:
    if not iso_day:
        return None
    today = today or datetime.now(ZoneInfo(local_timezone())).date()
    return (today - date.fromisoformat(iso_day)).days


def _row(task: dict, space_by_id: dict) -> dict:
    updated = _ms_to_local_date(task.get("date_updated"))
    return {
        "title": task.get("name", "(no title)"),
        "space": space_by_id.get((task.get("space") or {}).get("id"), ""),
        # Which List inside that Space. ClickUp returns it on every task, so
        # naming it costs nothing and stops a Space with several Lists reading
        # back as one undifferentiated pile.
        "list": (task.get("list") or {}).get("name", ""),
        "status": (task.get("status") or {}).get("status", ""),
        "tags": [t.get("name", "") for t in task.get("tags", [])],
        "priority": (task.get("priority") or {}).get("priority"),
        "updated": updated,
        "days_since_update": _days_since(updated),
    }


def _trim(text: str, limit: int) -> tuple:
    """Trim to a character limit on a whitespace boundary. Returns the text and
    how many characters were dropped, so the caller can say so rather than
    handing the model a silently shortened document."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text, 0
    cut = text[:limit].rsplit(None, 1)[0]
    return cut + "…", len(text) - len(cut)


def _client(api_key: str | None):
    """Resolve the token and the workspace once, for both entrypoints."""
    token = resolve_key("CLICKUP_API_TOKEN", api_key)
    if not token:
        return None, missing_key_error("CLICKUP_API_TOKEN")
    return token, None


def list_clickup_spaces(api_key: str = None) -> dict:
    """The Spaces on the ClickUp workspace, each with the Lists it holds and the
    statuses it defines.

    Its own entrypoint because it is what you run first when setting this up,
    and because it answers both "which List do I mean?" and "why did my status
    filter not match?" on its own."""
    token, err = _client(api_key)
    if err:
        return err
    try:
        team_id = _team_id(token)
        spaces = _spaces(token, team_id)
        # The Lists, which no other read path pays for. This tool answers
        # "what is there?", and a Space without its Lists does not answer it.
        for sp in spaces:
            sp["lists"] = [l["name"] for l in _lists(token, sp["id"])]
    except _ClickUpError as e:
        return {"error": str(e)}
    except Exception as e:
        return http_error(e)
    return {"workspace_id": team_id, "spaces": spaces}


# `list_name`, not `list`: the parameter is a List's name and calling it `list`
# would shadow the builtin inside every function that takes one. `space` has no
# such clash, so it keeps ClickUp's bare noun.
def list_clickup_tasks(space: str = None, list_name: str = None, status: str = None,
                       include_done: bool = False, api_key: str = None) -> dict:
    """Callable entrypoint used by the agent loop's tool dispatcher.

    Sorted most-recently-touched first, so what falls off the character budget
    is the quiet tail rather than what is moving.

    A List name narrows to one List. Naming a List needs a Space to look it up
    in, so `list_name` without `space` is refused rather than searched for across
    the workspace — two Spaces are free to hold Lists of the same name."""
    token, err = _client(api_key)
    if err:
        return err

    try:
        team_id = _team_id(token)
        spaces = _spaces(token, team_id)
        if not spaces:
            return {"item_count": 0, "items_shown": 0, "spaces": [], "items": []}
        chosen = _resolve_space(spaces, space)
        if list_name and list_name.strip() and len(chosen) > 1:
            raise _ClickUpError(
                f"say which Space '{list_name}' is in — List names are only "
                f"unique inside a Space. Spaces: "
                f"{', '.join(a['space'] for a in spaces)}"
            )
        list_ids = None
        if list_name and list_name.strip():
            home = chosen[0]
            found = _resolve_list(_lists(token, home["id"]), list_name, home["space"])
            list_ids = [found["id"]]
        wanted_status = _resolve_status(chosen, status) if status else None
        tasks = _fetch_tasks(token, team_id, [a["id"] for a in chosen], include_done,
                             list_ids=list_ids)
    except _ClickUpError as e:
        return {"error": str(e)}
    except Exception as e:
        return http_error(e)

    space_by_id = {a["id"]: a["space"] for a in spaces}
    rows = [_row(t, space_by_id) for t in tasks]
    if wanted_status:
        rows = [r for r in rows if r["status"] == wanted_status]
    # Undated items sink rather than sorting to the top as "" would.
    rows.sort(key=lambda r: r["updated"] or "0000-01-01", reverse=True)

    shown, used = [], 0
    for row in rows:
        used += len(str(row))
        if used > _MAX_LIST_CHARS and shown:
            break
        shown.append(row)

    out = {
        "item_count": len(rows),
        "items_shown": len(shown),
        "spaces": [a["space"] for a in spaces],
    }
    if not include_done:
        out["note"] = "Items in a done/closed status are excluded. Pass include_done to see them."
    if len(shown) < len(rows):
        out["partial"] = (
            f"Showing the {len(shown)} most recently updated of {len(rows)} items. "
            "Do not say these are all of them."
        )
    out["items"] = shown
    return out


def _find_task(tasks: list, title: str) -> tuple:
    """Resolve a title to exactly one task. Returns (task, None) or
    (None, error_dict).

    Forgiving — exact match first, then a unique substring — because the model
    is passing back a title the user said out loud, not an identifier it was
    handed. An ambiguous title returns the candidates rather than picking one:
    on a WRITE, guessing would change the wrong item, and the user would have
    no reason to look.

    Shared by read_clickup_task and all three write tools on purpose. A write
    that resolved a title differently from the read the user just did is
    exactly the kind of trap nobody tests for."""
    wanted = _slug(title)
    matches = [t for t in tasks if _slug(t.get("name", "")) == wanted]
    if not matches:
        matches = [t for t in tasks if wanted and wanted in _slug(t.get("name", ""))]
    if not matches:
        return None, {
            "error": f"no ClickUp task matching '{title}'. "
                     "Use list_clickup_tasks to see what exists."
        }
    if len(matches) > 1:
        return None, {
            "error": f"'{title}' matches {len(matches)} tasks — ask which one.",
            "candidates": [t.get("name", "") for t in matches[:10]],
        }
    return matches[0], None


def read_clickup_task(title: str, api_key: str = None) -> dict:
    """One backlog item in full, matched on its title.

    Matching is forgiving — exact first, then a unique substring — because the
    model is passing back a title the user typed, not an identifier it was
    given. An ambiguous title returns the candidates rather than picking one."""
    if not title or not title.strip():
        return {"error": "title must not be empty"}

    token, err = _client(api_key)
    if err:
        return err

    try:
        team_id = _team_id(token)
        spaces = _spaces(token, team_id)
        # include_done: reading a shipped item is a normal thing to ask about,
        # and it is exactly what the default listing hides.
        tasks = _fetch_tasks(token, team_id, [a["id"] for a in spaces], include_done=True)
    except _ClickUpError as e:
        return {"error": str(e)}
    except Exception as e:
        return http_error(e)

    task, err = _find_task(tasks, title)
    if err:
        return err
    space_by_id = {a["id"]: a["space"] for a in spaces}
    item = _row(task, space_by_id)
    item["created"] = _ms_to_local_date(task.get("date_created"))
    item["url"] = task.get("url", "")

    description, dropped = _trim(task.get("description") or task.get("text_content") or "",
                                 _MAX_DESCRIPTION_CHARS)
    item["description"] = description
    if dropped:
        item["description_truncated"] = f"{dropped} characters not shown"

    # Comments are a separate call and are optional context: a failure here
    # costs the comments, not the item.
    comments, used = [], 0
    try:
        raw = _get(f"/task/{task['id']}/comment", token).get("comments", [])
        for c in raw[:_MAX_COMMENTS]:
            text, _ = _trim(c.get("comment_text") or "", _MAX_COMMENT_CHARS)
            used += len(text)
            if used > _MAX_COMMENT_CHARS and comments:
                break
            comments.append({"date": _ms_to_local_date(c.get("date")), "text": text})
    except Exception:
        item["comments_error"] = "could not read comments"
    item["comments"] = comments

    return item


# --------------------------------------------------------------------------- #
# Writes. Everything below CHANGES ClickUp, and every one is in WRITE_TOOLS so
# it pauses for a tap on the phone before it runs.
#
# add_clickup_task and comment_on_clickup_task are also in UNATTENDED_EXCLUDED_TOOLS:
# read_clickup_task hands descriptions and comments back to the model, so free
# text a background job writes here is read into a FUTURE Wren prompt. That is
# the same durable, prompt-visible-state test that keeps remember/pin/
# write_skill out of unattended runs. move_clickup_task writes one value out of a fixed
# list the Space defines, so it carries no text and stays allowed.
# --------------------------------------------------------------------------- #

# A captured idea is a sentence, not a document. Long enough for the two or
# three sentences that make Step 4's matching work (docs/clickup.md), short
# enough that nothing pastes an article in by accident.
_MAX_NEW_TITLE_CHARS = 200
_MAX_NEW_DESCRIPTION_CHARS = 4000
_MAX_NEW_COMMENT_CHARS = 4000


def _writable(api_key: str):
    """Token, workspace and spaces — the three lookups every write starts with."""
    token, err = _client(api_key)
    if err:
        return None, None, None, err
    team_id = _team_id(token)
    return token, team_id, _spaces(token, team_id), None


def add_clickup_task(title: str, space: str, list_name: str = None,
                     description: str = None, tags: list = None,
                     priority: str = None, api_key: str = None) -> dict:
    """Create a Task on a List inside one Space.

    The status is NOT a parameter: a new Task starts at that Space's own first
    not-started status ('idea' in the Wren Space, 'to do' in the others), read
    off the Space. So the model never picks one, and never has to know that the
    Spaces use different words.

    `space` is required. There is no safe default, and a Task filed in the
    wrong Space is worse than a question — it is invisible exactly where the
    user goes looking for it.

    `list_name` is optional only because a Space with a single List names that
    List unambiguously. A Space holding several and no name given is refused,
    not defaulted."""
    if not title or not title.strip():
        return {"error": "title must not be empty"}
    if len(title) > _MAX_NEW_TITLE_CHARS:
        return {"error": f"title is longer than {_MAX_NEW_TITLE_CHARS} characters"}
    if not space or not space.strip():
        return {"error": "space is required. Call list_clickup_spaces to see the "
                         "Spaces and the Lists in each one."}

    try:
        token, team_id, spaces, err = _writable(api_key)
        if err:
            return err
        chosen = _resolve_space(spaces, space)[0]
        target = _resolve_list(_lists(token, chosen["id"]), list_name, chosen["space"])
        list_id = target["id"]
        payload = {"name": title.strip(), "status": _opening_status(chosen)}
        if description and description.strip():
            payload["description"] = description.strip()[:_MAX_NEW_DESCRIPTION_CHARS]
        if tags:
            payload["tags"] = [t for t in tags if t and t.strip()]
        if priority:
            payload["priority"] = _resolve_priority(priority)
        created = _write("POST", f"/list/{list_id}/task", token, payload)
    except _ClickUpError as e:
        return {"error": str(e)}
    except Exception as e:
        return http_error(e)

    return {
        # Every write says what it wrote and names itself, so a confirmation
        # chain cannot read one result as licence to do it again (docs/limits.md).
        "tool_name": "add_clickup_task",
        "created": True,
        "title": created.get("name", title),
        "space": chosen["space"],
        "list": target["name"],
        "status": (created.get("status") or {}).get("status", ""),
        "url": created.get("url", ""),
    }


def move_clickup_task(title: str, status: str, api_key: str = None) -> dict:
    """Move one backlog item to a different status.

    The status is validated against the statuses THAT ITEM'S OWN Space defines,
    not against a merged list. ClickUp statuses are per-Space and really do
    differ here, so an unvalidated move either 400s or lands somewhere nobody
    asked for."""
    if not title or not title.strip():
        return {"error": "title must not be empty"}
    if not status or not status.strip():
        return {"error": "status must not be empty"}

    try:
        token, team_id, spaces, err = _writable(api_key)
        if err:
            return err
        tasks = _fetch_tasks(token, team_id, [a["id"] for a in spaces],
                             include_done=True)
        task, err = _find_task(tasks, title)
        if err:
            return err
        space_id = (task.get("space") or {}).get("id")
        home = [a for a in spaces if a["id"] == space_id]
        if not home:
            return {"error": f"'{task.get('name', title)}' is in a Space these tools cannot see"}
        was = (task.get("status") or {}).get("status", "")
        wanted = _resolve_status(home, status)
        if _slug(was) == _slug(wanted):
            return {"tool_name": "move_clickup_task", "moved": False,
                    "title": task.get("name", title), "status": wanted,
                    "note": "it was already in that status; nothing was changed"}
        _write("PUT", f"/task/{task['id']}", token, {"status": wanted})
    except _ClickUpError as e:
        return {"error": str(e)}
    except Exception as e:
        return http_error(e)

    return {
        "tool_name": "move_clickup_task",
        "moved": True,
        "title": task.get("name", title),
        "space": home[0]["space"],
        "list": (task.get("list") or {}).get("name", ""),
        "from": was,
        "status": wanted,
        "url": task.get("url", ""),
    }


def comment_on_clickup_task(title: str, comment: str, api_key: str = None) -> dict:
    """Add a comment to one backlog item — where findings, links and decisions
    go, so the item carries its own history instead of it living in a chat log
    that is lost on restart."""
    if not title or not title.strip():
        return {"error": "title must not be empty"}
    if not comment or not comment.strip():
        return {"error": "comment must not be empty"}

    try:
        token, team_id, spaces, err = _writable(api_key)
        if err:
            return err
        tasks = _fetch_tasks(token, team_id, [a["id"] for a in spaces],
                             include_done=True)
        task, err = _find_task(tasks, title)
        if err:
            return err
        _write("POST", f"/task/{task['id']}/comment", token,
               {"comment_text": comment.strip()[:_MAX_NEW_COMMENT_CHARS],
                "notify_all": False})
    except _ClickUpError as e:
        return {"error": str(e)}
    except Exception as e:
        return http_error(e)

    return {
        "tool_name": "comment_on_clickup_task",
        "commented": True,
        "title": task.get("name", title),
        "url": task.get("url", ""),
    }


# How many rows the morning brief's two lists are allowed to run to. Both are
# read by eye over coffee, not by the model, so these are readability caps
# rather than context budgets — with an honest "+N more" when they bite.
_MAX_MOVED = 8
_MAX_IN_FLIGHT = 6

# How long an in-flight item has to sit before the brief calls it out by name.
# A week: short enough to catch something quietly stalling, long enough that a
# thing worked on last Friday isn't nagged about on Monday. Below this the
# callout is omitted entirely rather than repeating the freshest item back.
_STALE_DAYS = 7


def _stalest(in_flight: list) -> dict | None:
    """The in-flight item untouched longest, but only once it has been quiet for
    _STALE_DAYS. Returns None below that, and None for a single-item list whose
    one entry the section has already printed."""
    if len(in_flight) < 2:
        return None
    oldest = in_flight[-1]
    return oldest if (oldest.get("days_since_update") or 0) >= _STALE_DAYS else None


def clickup_digest(since_ms: int = None, api_key: str = None) -> dict:
    """What the morning brief needs, in one call. A **library function, not a
    chat tool** — deliberately no TOOL_SCHEMA, because in chat the same question
    is answered better by list_clickup_tasks, which can be asked follow-ups.

    Two halves, because they answer different questions:

    - `moved`: what changed since `since_ms` — the news. Includes finished
      items, since "X shipped" is the most interesting thing that can happen to
      a backlog item and excluding the Closed group would drop exactly that.
    - `in_flight`: what sits in ClickUp's **Active** status group right now,
      freshest first, plus `stalest` — the one that has gone longest untouched.
      Active is read from the Space's own status types rather than matched on
      status names, so this needs no edit when a Space's workflow changes.

    `checked_ms` is the cursor the caller persists after a successful send; it
    is captured BEFORE the fetch, so activity during the run is never skipped.
    """
    token, err = _client(api_key)
    if err:
        return err

    checked_ms = int(datetime.now().timestamp() * 1000)
    try:
        team_id = _team_id(token)
        spaces = _spaces(token, team_id)
        if not spaces:
            return {"moved": [], "in_flight": [], "stalest": None, "checked_ms": checked_ms}
        space_ids = [a["id"] for a in spaces]
        # Two calls, not one filtered locally: the moved window needs closed
        # items and the in-flight list must not have them.
        moved_raw = _fetch_tasks(token, team_id, space_ids, include_done=True,
                                 updated_after_ms=since_ms) if since_ms is not None else []
        current = _fetch_tasks(token, team_id, space_ids, include_done=False)
    except _ClickUpError as e:
        return {"error": str(e)}
    except Exception as e:
        return http_error(e)

    space_by_id = {a["id"]: a["space"] for a in spaces}
    # ClickUp's own grouping, read off each Space rather than matched on status
    # names: "open" is Not Started, "custom" is Active, "closed" is Done.
    group = {(a["id"], st["status"]): st["type"] for a in spaces for st in a["statuses"]}

    def _group_of(task):
        return group.get(((task.get("space") or {}).get("id"),
                          (task.get("status") or {}).get("status")))

    moved = []
    for t in moved_raw:
        row = _row(t, space_by_id)
        # Finishing beats being created: an item added and shipped inside the
        # same window is news because it shipped, and "added" would bury that.
        if _group_of(t) == "closed":
            row["change"] = row["status"]
        elif int(t.get("date_created") or 0) > (since_ms or 0):
            row["change"] = "added"
        else:
            row["change"] = f"now {row['status']}"
        moved.append(row)
    moved.sort(key=lambda r: r["updated"] or "0000-01-01", reverse=True)

    in_flight = [_row(t, space_by_id) for t in current if _group_of(t) == "custom"]
    in_flight.sort(key=lambda r: r["updated"] or "0000-01-01", reverse=True)

    return {
        "moved": moved[:_MAX_MOVED],
        "moved_total": len(moved),
        "in_flight": in_flight[:_MAX_IN_FLIGHT],
        "in_flight_total": len(in_flight),
        # The tail of the same sort, so it is always one of the in-flight items
        # even when the list above was capped — and only once it has actually
        # gone quiet, so the callout means something rather than naming the
        # thing worked on yesterday.
        "stalest": _stalest(in_flight),
        "checked_ms": checked_ms,
    }


def backlog_anchors(api_key: str = None) -> dict:
    """Open items that carry enough text to be matched against, for
    tasks/daily_synthesis.py. A **library function, not a chat tool** — same
    reason as clickup_digest: in chat the question is answered better by
    list_clickup_tasks, which can be asked follow-ups.

    One request no matter how big the backlog is. ClickUp returns a task's
    description on the list endpoint, so reading each item individually would
    cost one call per item against a 100/minute budget and learn nothing extra.

    **An item with no description is left out, and `skipped` counts them.** A
    bare title is not an anchor: it can only match its own spelling, which is
    the tautology gather_project_anchors already skips a name-only project for.
    It is worse than useless here — a two-word title is a two-token anchor, and
    short anchors win that matcher's normalized score, so bare titles would
    outrank the wiki while saying nothing. 25 of this account's 37 open items
    were bare when this was written, which is why the count is returned rather
    than swallowed: a backlog of nothing but titles makes this source contribute
    nothing, and that reads exactly like a broken matcher.

    Done items are excluded. A shipped idea is not something to be nudged
    toward."""
    token, err = _client(api_key)
    if err:
        return err

    try:
        team_id = _team_id(token)
        spaces = _spaces(token, team_id)
        if not spaces:
            return {"items": [], "skipped": 0}
        tasks = _fetch_tasks(token, team_id, [a["id"] for a in spaces],
                             include_done=False)
    except _ClickUpError as e:
        return {"error": str(e)}
    except Exception as e:
        return http_error(e)

    items, skipped = [], 0
    for task in tasks:
        description = (task.get("description") or "").strip()
        if not description:
            skipped += 1
            continue
        items.append({
            "title": task.get("name", "(no title)"),
            "description": description[:_MAX_DESCRIPTION_CHARS],
            "tags": [t.get("name", "") for t in task.get("tags", [])],
        })
    return {"items": items, "skipped": skipped}


def tagged_clickup_tasks(tags: list, api_key: str = None) -> dict:
    """Tasks currently carrying any of `tags`. A **library function, not a chat
    tool** — no TOOL_SCHEMA, because the only caller is tasks/clickup_watcher.py
    and the model has list_clickup_tasks for the same question in words.

    Several tags[] values are OR-ed by ClickUp (verified live), so this is one
    GET however many tags are watched.

    Unlike every other function here it returns the ClickUp task **id**. That is
    deliberate and it is why this is not a tool: the watcher removes the tag by
    id, and an id must never reach the model (docs/opaque-identifiers.md).
    Closed tasks are included — a tag on a shipped Task is still a request.
    """
    token, err = _client(api_key)
    if err:
        return err
    try:
        team_id = _team_id(token)
        spaces = _spaces(token, team_id)
        if not spaces:
            return {"tasks": []}
        raw = _fetch_tasks(token, team_id, [a["id"] for a in spaces],
                           include_done=True, tags=tags)
    except _ClickUpError as e:
        return {"error": str(e)}
    except Exception as e:
        return http_error(e)

    space_by_id = {a["id"]: a["space"] for a in spaces}
    wanted = {t.lower() for t in tags}
    out = []
    for t in raw:
        row = _row(t, space_by_id)
        row["id"] = t["id"]
        # Which of the watched tags this Task carries, in the order given, so a
        # Task wearing two of them gets one job under a settled first tag rather
        # than whichever ClickUp happened to list first.
        carried = {name.lower() for name in row["tags"]}
        row["watched"] = [tag for tag in tags if tag.lower() in carried and tag.lower() in wanted]
        row["description"], _ = _trim(t.get("description") or "", _MAX_DESCRIPTION_CHARS)
        out.append(row)
    return {"tasks": out}


def remove_clickup_tag(task_id: str, tag: str, api_key: str = None) -> dict:
    """Take one tag off one Task. A **library function, not a chat tool**, for
    the same reason as above — it takes an id.

    This is what stops the watcher acting on the same Task twice: the tag is the
    request, so removing it is how the request is marked as taken. It is a write
    but it carries no free text and no model-chosen value, which is the same
    reason move_clickup_task is allowed in unattended runs.

    **A tag name may not contain a slash.** ClickUp's router rejects it in this
    path — encoded as %2F or raw — with a plain-text "404 page not found" that
    never reaches their code, while hyphen, underscore, colon and dot all return
    200 (measured live 2026-08-27). There is no other way round it: ClickUp has
    no "set all tags" endpoint. This cost real time as a mystery 404 from a
    running watcher, so it is refused here with a message that says why rather
    than sent and lost.
    """
    if "/" in tag:
        return {"error": f"ClickUp cannot remove a tag with a slash in it ({tag!r}); "
                         "rename it to use a hyphen"}
    token, err = _client(api_key)
    if err:
        return err
    try:
        _write("DELETE", f"/task/{task_id}/tag/{quote(tag, safe='')}", token, None)
    except Exception as e:
        return http_error(e)
    return {"removed": tag, "task_id": task_id}


# --------------------------------------------------------------------------- #
# One Task in full, and the file hanging off it. Both take a ClickUp **id** or a
# ClickUp-supplied URL, which is exactly why neither is a chat tool — the same
# rule that keeps tagged_clickup_tasks and remove_clickup_tag schema-less
# (docs/opaque-identifiers.md). Their only caller is tasks/clickup_watcher.py.
# --------------------------------------------------------------------------- #

# A Claude Code plan runs to a few thousand characters; the one already attached
# to a live Task is 6KB. This ceiling exists so a mis-attached video or database
# dump cannot be pulled into memory, not because a real plan approaches it.
_MAX_ATTACHMENT_BYTES = 400_000

# Read in chunks so the ceiling above is enforced while the download is still
# happening. Checking Content-Length alone trusts a header the sender chose.
_ATTACHMENT_CHUNK = 64 * 1024


def clickup_task_detail(task_id: str, api_key: str = None) -> dict:
    """One Task by id, including its **attachments**.

    Its own entrypoint because GET /team/{id}/task does not return attachments
    at all — the field is absent from that response, not merely empty (verified
    against the live workspace 2026-08-31). So the only way to learn whether a
    Task carries a file is to ask for that one Task, which is why the watcher
    pays one extra GET per tagged Task rather than reading it off the poll.
    """
    token, err = _client(api_key)
    if err:
        return err
    try:
        task = _get(f"/task/{task_id}", token)
    except Exception as e:
        return http_error(e)

    attachments = []
    for a in task.get("attachments") or []:
        # ClickUp keeps deleted attachments in the array with deleted=True.
        if a.get("deleted"):
            continue
        attachments.append({
            "id": a.get("id", ""),
            "title": a.get("title", ""),
            "extension": (a.get("extension") or "").lower(),
            "size": a.get("size"),
            "url": a.get("url", ""),
        })

    description, _ = _trim(task.get("description") or task.get("text_content") or "",
                           _MAX_DESCRIPTION_CHARS)
    return {
        "id": task.get("id", task_id),
        "title": task.get("name", ""),
        "status": (task.get("status") or {}).get("status", ""),
        "description": description,
        "attachments": attachments,
        "url": task.get("url", ""),
    }


def download_attachment(url: str, max_bytes: int = _MAX_ATTACHMENT_BYTES) -> dict:
    """Fetch one attachment as text. Returns {"text": ...} or {"error": ...}.

    **The URL is scheme-checked before it is fetched**, even though ClickUp
    supplied it: this module already treats ClickUp as a place other people will
    eventually write (see the guest-access note in docs/clickup.md), and an
    attachment record is one more field that arrives over the wire. https only.

    Not a chat tool, and deliberately not general-purpose: agent/tools/fetch.py
    is how the model reads a web page. This exists so the watcher can read a
    plan file without the model ever seeing a URL it could be talked into
    following.
    """
    if not (url or "").lower().startswith("https://"):
        return {"error": "attachment URL is not https"}
    try:
        with requests.get(url, timeout=TIMEOUT_S, stream=True) as resp:
            resp.raise_for_status()
            body = b""
            for chunk in resp.iter_content(_ATTACHMENT_CHUNK):
                body += chunk
                if len(body) > max_bytes:
                    return {"error": f"attachment is larger than {max_bytes} bytes"}
    except Exception as e:
        return http_error(e)
    try:
        return {"text": body.decode("utf-8")}
    except UnicodeDecodeError:
        return {"error": "attachment is not UTF-8 text"}


# ClickUp's own three nouns, used exactly as ClickUp uses them, because the
# model is repeating back a word the user said: a **Space** is a top-level area
# of the workspace, a **List** sits inside a Space, and a **Task** sits on a
# List. Nothing here assumes a Space, List or Task is called anything in
# particular — the names come from the account on every call.
#
# Every tool is named "clickup" so the model can tell these apart from Google
# Tasks' create_task/list_tasks, which are always loaded and own the bare word
# "task". The name is the separator; the keyword gate is only the pre-loader.

LIST_CLICKUP_SPACES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_clickup_spaces",
        "description": (
            f"List the Spaces in {_NAME}'s ClickUp workspace, the Lists inside each "
            "Space, and the statuses each Space defines. Call this first whenever you "
            "need a Space name, a List name or a status and do not already have one "
            "from an earlier tool result in this conversation. His workspace is NOT "
            "something you know: only the Spaces and Lists this tool returns exist. "
            "Never name a Space or List from your own knowledge, never guess one, and "
            "if the tool returns nothing, say the workspace is empty."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

LIST_CLICKUP_TASKS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_clickup_tasks",
        "description": (
            f"List Tasks in {_NAME}'s ClickUp — the ideas, bugs, features and pieces of "
            "work he tracks. Each Task comes back with the Space and List it sits in, "
            "its status, its tags and when it last moved. Filter by Space, by List, or "
            "by status, or call it with nothing to see everything. This is ClickUp, not "
            "his Google Tasks list — for a dated personal chore use list_tasks instead. "
            "His ClickUp is NOT something you know: only the Tasks this tool returns "
            "exist. Never name a Task from your own knowledge, never guess that one is "
            "there, and if the tool returns nothing, say there are none. Use "
            "read_clickup_task for the detail on any one of them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "space": {
                    "type": "string",
                    "description": (
                        "Limit to one Space, by name. Omit to cover every Space. The "
                        "Spaces that exist are the ones this tool reports back in "
                        "'spaces', or the ones list_clickup_spaces returns — call one "
                        "of them first if you do not know the names."
                    ),
                },
                "list_name": {
                    "type": "string",
                    "description": (
                        "Limit to one List inside that Space, by name. Requires 'space' "
                        "as well, because two Spaces may hold Lists of the same name. "
                        "Omit to cover every List in the Space."
                    ),
                },
                "status": {
                    "type": "string",
                    "description": (
                        "Limit to one status. Statuses are defined per Space and really "
                        "do differ between them; if the status does not exist the tool "
                        "tells you which ones do."
                    ),
                },
                "include_done": {
                    "type": "boolean",
                    "description": (
                        "Include finished Tasks (shipped, complete). Off by default, so "
                        "the result is live work. Turn it on to answer what he has "
                        "already delivered."
                    ),
                },
            },
        },
    },
}

READ_CLICKUP_TASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_clickup_task",
        "description": (
            f"Read one Task in {_NAME}'s ClickUp in full: its description, its comments, "
            "its status, tags, priority, the Space and List it sits in, when it was "
            "created and last touched, and the link to open it. Pass the Task's title — "
            "part of the title is enough. Only Tasks list_clickup_tasks returns exist; "
            "never invent a title, and never answer from your own knowledge of what he "
            "might be working on."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "The Task's title, or a distinctive part of it.",
                },
            },
            "required": ["title"],
        },
    },
}

ADD_CLICKUP_TASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "add_clickup_task",
        "description": (
            f"Create a new Task in {_NAME}'s ClickUp — an idea, a bug, a feature, a "
            "piece of work he wants tracked. Call this in the same turn he asks for it: "
            "do not describe the Task you would create and wait, because the app asks "
            "him to confirm before anything is written. The Space is required and must "
            "be one list_clickup_spaces or list_clickup_tasks returns; call one of them "
            "first if you do not know the names. Do not choose a status — a new Task "
            "always starts not-started. This is ClickUp, not his Google Tasks list: for "
            "a dated personal chore use create_task instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "One line naming the Task, as he said it.",
                },
                "space": {
                    "type": "string",
                    "description": "Which Space it belongs in, by name.",
                },
                "list_name": {
                    "type": "string",
                    "description": (
                        "Which List inside that Space, by name. Only needed when the "
                        "Space holds more than one List — if it does and you leave this "
                        "out, the tool refuses and names the Lists rather than guessing."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "Two or three sentences on what it is and why. Worth writing: a "
                        "bare title makes this Task invisible to later matching."
                    ),
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional labels, e.g. bug, feature, maintenance.",
                },
                "priority": {
                    "type": "string",
                    "enum": ["urgent", "high", "normal", "low"],
                    "description": "Optional. Use the word, never a number.",
                },
            },
            "required": ["title", "space"],
        },
    },
}

MOVE_CLICKUP_TASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "move_clickup_task",
        "description": (
            "Move one ClickUp Task to a different status — for example when "
            f"{_NAME} says something is now being built, is parked, or has shipped. "
            "Call it in the same turn he asks; the app asks him to confirm before "
            "anything changes. Statuses are defined per Space and differ between them, "
            "so if you are unsure what a Task's Space allows, call read_clickup_task or "
            "list_clickup_tasks first rather than guessing — a wrong status is refused "
            "and the error names the valid ones."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "The Task's title, or a distinctive part of it.",
                },
                "status": {
                    "type": "string",
                    "description": "The status to move it to, e.g. building, parked, shipped.",
                },
            },
            "required": ["title", "status"],
        },
    },
}

COMMENT_ON_CLICKUP_TASK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "comment_on_clickup_task",
        "description": (
            "Add a comment to one ClickUp Task — findings, a link, a decision, or what "
            "you worked out. Use this to record something ON the Task rather than only "
            f"telling {_NAME} in chat, because chat is lost on restart and the Task is "
            "not. Call it in the same turn; the app asks him to confirm before it is "
            "written."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "The Task's title, or a distinctive part of it.",
                },
                "comment": {
                    "type": "string",
                    "description": "The note to add, in full sentences.",
                },
            },
            "required": ["title", "comment"],
        },
    },
}

CLICKUP_TOOL_SCHEMAS = [
    LIST_CLICKUP_SPACES_SCHEMA,
    LIST_CLICKUP_TASKS_SCHEMA,
    READ_CLICKUP_TASK_SCHEMA,
    ADD_CLICKUP_TASK_SCHEMA,
    MOVE_CLICKUP_TASK_SCHEMA,
    COMMENT_ON_CLICKUP_TASK_SCHEMA,
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",
                        choices=["spaces", "tasks", "read", "digest", "add", "move", "comment"])
    parser.add_argument("--space", default=None)
    parser.add_argument("--list", dest="list_name", default=None)
    parser.add_argument("--status", default=None)
    parser.add_argument("--include-done", dest="include_done", action="store_true")
    parser.add_argument("--title", default=None)
    parser.add_argument("--since-days", dest="since_days", type=int, default=1,
                        help="digest: how far back the 'what moved' window looks")
    parser.add_argument("--description", default=None)
    parser.add_argument("--tag", dest="tags", action="append", default=None)
    parser.add_argument("--priority", default=None)
    parser.add_argument("--comment", default=None)
    parser.add_argument("--api-key", dest="api_key", default=None)
    args = parser.parse_args()

    if args.command == "spaces":
        return print_result(list_clickup_spaces(args.api_key))
    if args.command == "digest":
        since_ms = int((datetime.now() - timedelta(days=args.since_days)).timestamp() * 1000)
        return print_result(clickup_digest(since_ms, args.api_key))
    if args.command == "read":
        if not args.title:
            parser.error("read needs --title")
        return print_result(read_clickup_task(args.title, args.api_key))
    if args.command == "add":
        if not args.title or not args.space:
            parser.error("add needs --title and --space")
        return print_result(add_clickup_task(args.title, args.space, args.list_name,
                                             args.description, args.tags,
                                             args.priority, args.api_key))
    if args.command == "move":
        if not args.title or not args.status:
            parser.error("move needs --title and --status")
        return print_result(move_clickup_task(args.title, args.status, args.api_key))
    if args.command == "comment":
        if not args.title or not args.comment:
            parser.error("comment needs --title and --comment")
        return print_result(comment_on_clickup_task(args.title, args.comment, args.api_key))
    return print_result(list_clickup_tasks(args.space, args.list_name, args.status,
                                           args.include_done, args.api_key))


if __name__ == "__main__":
    sys.exit(main())
