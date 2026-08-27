"""Read the ClickUp backlog — the ideas, bugs and features tracked per project.

Read-only. Two tools: list_backlog (what's on it, filterable by area and
status) and read_backlog_item (one item in full, with its description and
comments). Writes are a later, separately gated step; nothing here changes
anything in ClickUp.

An **area** is a ClickUp Space, addressed by a slug of its name ("wren",
"vibefoundry", "blog") rather than by its id. Areas are discovered from the
account on every call instead of being pinned in config: it is one extra GET,
and it means adding or renaming a Space needs no edit here and no restart. The
model therefore never sees, and never has to copy back, a ClickUp id — same
reasoning as read_project taking a project name (docs/opaque-identifiers.md).

Statuses are per-Space in ClickUp and genuinely differ between them (the Wren
Space runs idea/designed/building/parked/shipped; the others to do/in
progress/complete), so a status filter is validated against the statuses the
chosen area actually has, and the error names them.

Usage:
    python -m agent.tools.clickup areas
    python -m agent.tools.clickup list [--area wren] [--status parked] [--include-done]
    python -m agent.tools.clickup read --title "starred releases"

Key resolution order: --api-key arg > config/.env file > CLICKUP_API_TOKEN env var
"""

import argparse
import re
import sys
from datetime import date, datetime
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
# and this account already holds 57 items across three areas — 6800 chars
# against the loop's 8000 default, before a single new idea is captured. A row
# cap would not have caught that: it bounds how many rows come back, never how
# big they are (docs/row-caps-need-char-budgets is the standing version of this).
_MAX_LIST_CHARS = 6000

# read_backlog_item returns one item the user asked for, so it gets more room
# than a listing — but a description runs to over a thousand characters already
# and comments are unbounded, so both are trimmed here rather than left to the
# loop's backstop, which would slice mid-sentence and say nothing about it.
_MAX_DESCRIPTION_CHARS = 2000
_MAX_COMMENTS = 10
_MAX_COMMENT_CHARS = 2500


class _ClickUpError(Exception):
    """A configuration- or lookup-shaped failure with a message meant to be
    read: no workspace, an unknown area, an unknown status. Distinct from a
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


def _areas(token: str, team_id: str) -> list:
    """Every Space on the workspace, as an area: slug, display name, id, and
    the statuses that Space actually defines."""
    spaces = _get(f"/team/{team_id}/space", token, archived="false").get("spaces", [])
    return [
        {
            "area": _slug(s.get("name", "")),
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


def _resolve_area(areas: list, area: str) -> list:
    """The one area named, or all of them when nothing was named."""
    if not area or not area.strip():
        return areas
    wanted = _slug(area)
    match = [a for a in areas if a["area"] == wanted]
    if not match:
        known = ", ".join(a["area"] for a in areas)
        raise _ClickUpError(f"no area named '{area}'. Areas: {known}")
    return match


def _resolve_status(chosen: list, status: str) -> str:
    """Validate a status against what the chosen areas actually define, and
    hand back its canonical spelling. ClickUp statuses are per-Space, so an
    unvalidated filter silently returns nothing rather than saying it was
    given a status that does not exist here."""
    wanted = _slug(status)
    for a in chosen:
        for st in a["statuses"]:
            if _slug(st["status"]) == wanted:
                return st["status"]
    known = sorted({st["status"] for a in chosen for st in a["statuses"]})
    where = chosen[0]["area"] if len(chosen) == 1 else "these areas"
    raise _ClickUpError(f"no status '{status}' in {where}. Statuses: {', '.join(known)}")


def _fetch_tasks(token: str, team_id: str, space_ids: list, include_done: bool) -> list:
    """Every task in the given Spaces, paged. ClickUp excludes its Closed
    status group by default and that is not a rounding error here — 21 of this
    account's 57 items are shipped — so include_done is the difference between
    "the backlog" and "everything ever tracked", not a nicety."""
    tasks, page = [], 0
    while page < _MAX_PAGES:
        params = {"page": page, "space_ids[]": space_ids}
        if include_done:
            params["include_closed"] = "true"
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


def _row(task: dict, area_by_id: dict) -> dict:
    updated = _ms_to_local_date(task.get("date_updated"))
    return {
        "title": task.get("name", "(no title)"),
        "area": area_by_id.get((task.get("space") or {}).get("id"), ""),
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


def list_areas(api_key: str = None) -> dict:
    """The Spaces on the ClickUp workspace, with the statuses each one defines.

    Its own entrypoint because it is what you run first when setting this up,
    and because it answers "why did my status filter not match?" on its own."""
    token, err = _client(api_key)
    if err:
        return err
    try:
        team_id = _team_id(token)
        areas = _areas(token, team_id)
    except _ClickUpError as e:
        return {"error": str(e)}
    except Exception as e:
        return http_error(e)
    return {"workspace_id": team_id, "areas": areas}


def list_backlog(area: str = None, status: str = None, include_done: bool = False,
                 api_key: str = None) -> dict:
    """Callable entrypoint used by the agent loop's tool dispatcher.

    Sorted most-recently-touched first, so what falls off the character budget
    is the quiet tail rather than what is moving."""
    token, err = _client(api_key)
    if err:
        return err

    try:
        team_id = _team_id(token)
        areas = _areas(token, team_id)
        if not areas:
            return {"item_count": 0, "items_shown": 0, "areas": [], "items": []}
        chosen = _resolve_area(areas, area)
        wanted_status = _resolve_status(chosen, status) if status else None
        tasks = _fetch_tasks(token, team_id, [a["id"] for a in chosen], include_done)
    except _ClickUpError as e:
        return {"error": str(e)}
    except Exception as e:
        return http_error(e)

    area_by_id = {a["id"]: a["area"] for a in areas}
    rows = [_row(t, area_by_id) for t in tasks]
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
        "areas": [a["area"] for a in areas],
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


def read_backlog_item(title: str, api_key: str = None) -> dict:
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
        areas = _areas(token, team_id)
        # include_done: reading a shipped item is a normal thing to ask about,
        # and it is exactly what the default listing hides.
        tasks = _fetch_tasks(token, team_id, [a["id"] for a in areas], include_done=True)
    except _ClickUpError as e:
        return {"error": str(e)}
    except Exception as e:
        return http_error(e)

    wanted = _slug(title)
    matches = [t for t in tasks if _slug(t.get("name", "")) == wanted]
    if not matches:
        matches = [t for t in tasks if wanted and wanted in _slug(t.get("name", ""))]
    if not matches:
        return {"error": f"no backlog item matching '{title}'. Use list_backlog to see what exists."}
    if len(matches) > 1:
        return {
            "error": f"'{title}' matches {len(matches)} items — ask which one.",
            "candidates": [t.get("name", "") for t in matches[:10]],
        }

    task = matches[0]
    area_by_id = {a["id"]: a["area"] for a in areas}
    item = _row(task, area_by_id)
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


LIST_BACKLOG_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_backlog",
        "description": (
            f"List what is on {_NAME}'s backlog in ClickUp — the ideas, bugs, features and "
            "pieces of work he tracks, grouped into areas (one per project or topic). Each "
            "item comes back with its area, its status, its tags and when it last moved. "
            "His backlog is NOT something you know: only the items this tool returns exist. "
            "Never name an item from your own knowledge, never guess that one is on there, "
            "and if the tool returns nothing, say the backlog is empty. Use read_backlog_item "
            "for the detail on any one of them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "area": {
                    "type": "string",
                    "description": (
                        "Limit to one area, e.g. 'wren'. Omit to list every area. The areas "
                        "that exist are the ones this tool reports back in 'areas' — call it "
                        "without an area first if you don't know them."
                    ),
                },
                "status": {
                    "type": "string",
                    "description": (
                        "Limit to one status, e.g. 'parked'. Statuses differ between areas; "
                        "if the status doesn't exist the tool tells you which ones do."
                    ),
                },
                "include_done": {
                    "type": "boolean",
                    "description": (
                        "Include finished items (shipped, complete). Off by default, so the "
                        "result is the live backlog. Turn it on to answer what he has "
                        "already delivered."
                    ),
                },
            },
        },
    },
}

READ_BACKLOG_ITEM_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_backlog_item",
        "description": (
            f"Read one item on {_NAME}'s ClickUp backlog in full: its description, its "
            "comments, its status, tags, priority, when it was created and last touched, "
            "and the link to open it. Pass the item's title — part of the title is enough. "
            "Only items list_backlog returns exist; never invent a title, and never answer "
            "from your own knowledge of what he might be working on."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "The item's title, or a distinctive part of it.",
                },
            },
            "required": ["title"],
        },
    },
}

BACKLOG_TOOL_SCHEMAS = [LIST_BACKLOG_SCHEMA, READ_BACKLOG_ITEM_SCHEMA]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["areas", "list", "read"])
    parser.add_argument("--area", default=None)
    parser.add_argument("--status", default=None)
    parser.add_argument("--include-done", dest="include_done", action="store_true")
    parser.add_argument("--title", default=None)
    parser.add_argument("--api-key", dest="api_key", default=None)
    args = parser.parse_args()

    if args.command == "areas":
        return print_result(list_areas(args.api_key))
    if args.command == "read":
        if not args.title:
            parser.error("read needs --title")
        return print_result(read_backlog_item(args.title, args.api_key))
    return print_result(list_backlog(args.area, args.status, args.include_done, args.api_key))


if __name__ == "__main__":
    sys.exit(main())
