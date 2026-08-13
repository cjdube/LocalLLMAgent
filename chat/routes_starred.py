"""The /starred JSON API — the starred-repo list the page renders.

Everything the view shows apart from the repo list itself is a cached store
written by a nightly task: blurbs (tasks/starred_blurbs.py), latest releases
(tasks/starred_releases.py), installed versions (tasks/starred_installed.py).
The model never runs on this request path.

The repo list is the one live read, because a star added an hour ago should show
up. But live is not the same as required: GitHub paginates, so a slow or rate-
limited API used to leave the page completely empty while three caches sat on
disk holding most of what it renders. So the list is cached too — written by
starred_releases, which already walks it nightly — and used as the fallback when
the live fetch fails. A stale list beats no page.

Registered as a Flask blueprint by chat/server.py.
"""

import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify

from agent.store import load_json
from agent.tools.github_starred import compare_versions, fetch_starred_repos
from chat.auth import _authenticated
from tasks import starred_blurbs, starred_installed, starred_releases

logger = logging.getLogger("wren")

starred_bp = Blueprint("starred", __name__)

# A release cut within this many days is badged "new" on /starred. Recency —
# rather than per-visit "seen" tracking — keeps the endpoint a pure read: no
# mutating GET, no seen-state store.
RECENT_RELEASE_DAYS = 30


def _release_is_new(published_at: str) -> bool:
    """True if the release was published within RECENT_RELEASE_DAYS. Compares
    timezone-aware UTC on both sides — GitHub timestamps are UTC, and we never
    slice the ISO string against a local calendar day (per the timestamp policy);
    a missing or unparseable timestamp is simply not new."""
    if not published_at:
        return False
    try:
        published_dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(timezone.utc) - published_dt <= timedelta(days=RECENT_RELEASE_DAYS)


def _repo_list() -> tuple[list, str | None, str | None]:
    """(repos, error, fetched_at) — the live star list, or the cached one.

    `error` is set only when BOTH the live fetch and the cache fail, since a
    served-from-cache page isn't an error state; `fetched_at` is set only when
    the answer came from the cache, and is what the page shows as staleness.
    """
    result = fetch_starred_repos()
    if "error" not in result:
        return result.get("repos", []), None, None

    cached = load_json(starred_releases.REPOS_PATH, {})
    repos = cached.get("repos") or []
    if not repos:
        return [], result["error"], None
    logger.warning("api_starred: live fetch failed (%s); serving the cached list "
                   "from %s", result["error"], cached.get("fetched_at"))
    return repos, None, cached.get("fetched_at")


@starred_bp.route("/api/starred", methods=["GET"])
def api_starred():
    """Live list of starred repos, each with its cached "what it does" blurb
    (falling back to the repo's GitHub description for any not yet cached by
    tasks/starred_blurbs.py), its cached latest release (tasks/starred_releases.py),
    and the user's cached installed version (tasks/starred_installed.py) with an
    update-available flag when that version is behind the latest release. The
    model never runs on this request path — the blurbs, releases, and installed
    versions are read from their stores — so the page stays instant.

    `stale` is True when the repo list itself came from the cache because the
    live GitHub fetch failed; `fetched_at` then says how old it is."""
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    repos, error, fetched_at = _repo_list()
    if error:
        return jsonify({"repos": [], "error": error})
    blurbs = load_json(starred_blurbs.BLURBS_PATH, {})
    releases = load_json(starred_releases.RELEASES_PATH, {})
    installed = load_json(starred_installed.INSTALLED_PATH, {})
    for r in repos:
        cached = blurbs.get(r["full_name"], {}).get("blurb")
        r["blurb"] = cached or r.get("description") or ""
        release = releases.get(r["full_name"])
        r["latest_release"] = release or None
        r["release_is_new"] = bool(release) and _release_is_new(release.get("published_at"))
        # Installed version (only the repos the user tracks in starred_installed.json
        # have an entry). update_available is True only when we can confidently
        # place the installed version behind the latest release tag; a missing
        # side or an uncomparable scheme leaves it None (no false alarm).
        inst = installed.get(r["full_name"]) or {}
        r["installed_version"] = inst.get("version")
        r["installed_error"] = inst.get("error")
        r["update_available"] = compare_versions(inst.get("version"), (release or {}).get("tag"))
    body = {"repos": repos}
    if fetched_at is not None:
        body["stale"] = True
        body["fetched_at"] = fetched_at
    return jsonify(body)
