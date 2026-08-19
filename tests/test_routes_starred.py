"""Tests for chat/routes_starred.py — the /starred JSON API.

Covers the three cached merges the page depends on (blurb, latest release,
installed version) and the fallback that keeps the page from going blank when
GitHub is unreachable. fetch_starred_repos is always stubbed; the three stores
are redirected to tmp_path suite-wide by tests/conftest.py, so nothing here
reads or writes the real config/.
"""

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("WREN_CHAT_TOKEN", "test-token")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import pytest

from agent.store import atomic_write_json
from agent.tools import github_starred
from chat import routes_starred as rs
from chat import server as srv


@pytest.fixture
def auth_client():
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:
        with c.session_transaction() as sess:
            sess["authenticated"] = True
            sess["sid"] = "test-sid"
        yield c


def _live(repos):
    # **kw, because the route passes its own timeout (LIVE_FETCH_TIMEOUT_S).
    return lambda **kw: {"repos": repos}


def _fails(error="rate limited"):
    return lambda **kw: {"error": error}


# --------------------------------------------------------------------------- #
# the cached merges
# --------------------------------------------------------------------------- #

def test_merges_cached_blurb_and_falls_back_to_description(auth_client, monkeypatch):
    monkeypatch.setattr(rs, "fetch_starred_repos", _live([
        {"full_name": "a/one", "description": "desc one", "language": "Rust"},
        {"full_name": "b/two", "description": "desc two", "language": "Go"},
    ]))
    # a/one is cached; b/two is not, so it falls back to its GitHub description.
    atomic_write_json(rs.starred_blurbs.BLURBS_PATH,
                      {"a/one": {"blurb": "cached blurb", "generated_at": "x"}})

    resp = auth_client.get("/api/starred")
    assert resp.status_code == 200
    repos = {r["full_name"]: r["blurb"] for r in resp.get_json()["repos"]}
    assert repos == {"a/one": "cached blurb", "b/two": "desc two"}


def test_merges_cached_release_with_new_badge(auth_client, monkeypatch):
    monkeypatch.setattr(rs, "fetch_starred_repos", _live([
        {"full_name": "a/recent", "description": "d", "language": "Rust"},
        {"full_name": "b/old", "description": "d", "language": "Go"},
        {"full_name": "c/none", "description": "d", "language": "C"},
    ]))
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    atomic_write_json(rs.starred_releases.RELEASES_PATH, {
        "a/recent": {"tag": "v1.3", "name": "1.3", "published_at": recent, "html_url": "u"},
        "b/old": {"tag": "v0.9", "name": "0.9", "published_at": old, "html_url": "u"},
    })

    resp = auth_client.get("/api/starred")
    assert resp.status_code == 200
    repos = {r["full_name"]: r for r in resp.get_json()["repos"]}
    assert repos["a/recent"]["latest_release"]["tag"] == "v1.3"
    assert repos["a/recent"]["release_is_new"] is True
    assert repos["b/old"]["release_is_new"] is False        # released, but outside the window
    assert repos["c/none"]["latest_release"] is None         # no cached release
    assert repos["c/none"]["release_is_new"] is False


def test_merges_installed_version_and_update_flag(auth_client, monkeypatch):
    monkeypatch.setattr(rs, "fetch_starred_repos", _live([
        {"full_name": "a/outdated", "description": "d", "language": "Rust"},
        {"full_name": "b/current", "description": "d", "language": "Go"},
        {"full_name": "c/untracked", "description": "d", "language": "C"},
        {"full_name": "d/broken", "description": "d", "language": "C"},
    ]))
    atomic_write_json(rs.starred_releases.RELEASES_PATH, {
        "a/outdated": {"tag": "v1.3.0", "name": "1.3.0", "published_at": None, "html_url": "u"},
        "b/current": {"tag": "v2.0.0", "name": "2.0.0", "published_at": None, "html_url": "u"},
    })
    atomic_write_json(rs.starred_installed.INSTALLED_PATH, {
        "a/outdated": {"version": "1.1.0", "source": "cmd", "error": None},
        "b/current": {"version": "v2.0.0", "source": "manual", "error": None},
        "d/broken": {"version": None, "source": "cmd", "error": "FileNotFoundError: rtk"},
    })

    resp = auth_client.get("/api/starred")
    assert resp.status_code == 200
    repos = {r["full_name"]: r for r in resp.get_json()["repos"]}
    assert repos["a/outdated"]["installed_version"] == "1.1.0"
    assert repos["a/outdated"]["update_available"] is True       # 1.1.0 < v1.3.0
    assert repos["b/current"]["update_available"] is False        # v2.0.0 == v2.0.0
    assert repos["c/untracked"]["installed_version"] is None      # not tracked
    assert repos["c/untracked"]["update_available"] is None
    assert repos["d/broken"]["installed_version"] is None
    assert repos["d/broken"]["installed_error"] == "FileNotFoundError: rtk"


def test_a_fresh_fetch_is_not_marked_stale(auth_client, monkeypatch):
    monkeypatch.setattr(rs, "fetch_starred_repos",
                        _live([{"full_name": "a/one", "description": "d"}]))
    body = auth_client.get("/api/starred").get_json()
    assert "stale" not in body and "fetched_at" not in body


# --------------------------------------------------------------------------- #
# the cached-list fallback — GitHub paginates, so a slow or rate-limited API
# used to blank the page while three caches sat on disk
# --------------------------------------------------------------------------- #

def test_a_failed_fetch_serves_the_cached_list_and_still_merges(auth_client, monkeypatch):
    monkeypatch.setattr(rs, "fetch_starred_repos", _fails())
    atomic_write_json(rs.starred_releases.REPOS_PATH, {
        "fetched_at": "2026-08-11T20:00:00+00:00",
        "repos": [{"full_name": "a/one", "description": "desc one", "language": "Rust"}],
    })
    atomic_write_json(rs.starred_blurbs.BLURBS_PATH,
                      {"a/one": {"blurb": "cached blurb", "generated_at": "x"}})

    resp = auth_client.get("/api/starred")
    assert resp.status_code == 200
    body = resp.get_json()
    # Serving from cache is not an error state — the page renders normally.
    assert "error" not in body
    assert body["stale"] is True
    assert body["fetched_at"] == "2026-08-11T20:00:00+00:00"
    # The blurb/release/installed merge still runs over the cached list.
    assert body["repos"][0]["blurb"] == "cached blurb"


def test_a_failed_fetch_with_no_cache_surfaces_the_error(auth_client, monkeypatch):
    # Nothing cached yet (first run, or a fresh checkout): the real error is more
    # useful than an empty page with no explanation.
    monkeypatch.setattr(rs, "fetch_starred_repos", _fails())

    body = auth_client.get("/api/starred").get_json()
    assert body["error"] == "rate limited"
    assert body["repos"] == []
    assert "stale" not in body


def test_the_live_fetch_uses_the_tight_request_path_timeout(auth_client, monkeypatch):
    # The fallback only triggers on an ERROR, so a GitHub that is slow but
    # succeeding would otherwise hold the request open for the scheduled tasks'
    # 15s while a complete list sits on disk. The tight timeout is what turns
    # "slow" into the error the fallback already handles.
    seen = {}
    monkeypatch.setattr(rs, "fetch_starred_repos",
                        lambda **kw: seen.update(kw) or {"repos": []})
    auth_client.get("/api/starred")
    assert seen["timeout"] == rs.LIVE_FETCH_TIMEOUT_S
    assert rs.LIVE_FETCH_TIMEOUT_S < github_starred.LIST_TIMEOUT_S


def test_an_empty_cached_list_counts_as_no_cache(auth_client, monkeypatch):
    # A store written with zero repos must not read as "the user stars nothing" —
    # that would show a blank page with no error and no way to tell why.
    monkeypatch.setattr(rs, "fetch_starred_repos", _fails())
    atomic_write_json(rs.starred_releases.REPOS_PATH,
                      {"fetched_at": "2026-08-11T20:00:00+00:00", "repos": []})

    body = auth_client.get("/api/starred").get_json()
    assert body["error"] == "rate limited"


# --------------------------------------------------------------------------- #
# _release_is_new — UTC on both sides, never an ISO slice against a local day
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("published,expected", [
    ("", False),
    (None, False),
    ("not-a-timestamp", False),
    ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), True),
    ((datetime.now(timezone.utc) - timedelta(days=400)).isoformat(), False),
])
def test_release_is_new_degrades_on_unusable_timestamps(published, expected):
    assert rs._release_is_new(published) is expected


def test_release_is_new_accepts_githubs_z_suffix():
    # GitHub sends "2026-08-11T20:00:00Z"; fromisoformat needs +00:00 on <3.11.
    recent = datetime.now(timezone.utc) - timedelta(days=3)
    stamp = recent.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert rs._release_is_new(stamp) is True
