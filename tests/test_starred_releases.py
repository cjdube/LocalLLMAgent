"""Tests for tasks/starred_releases.py — main() caches each starred repo's
latest release, skips repos with no release, prunes de-starred entries (the
whole cache is rewritten each run), and degrades on a fetch error. Collaborators
are monkeypatched; no GitHub access. The release store is redirected to tmp_path
by conftest."""

import pytest

from agent.store import atomic_write_json, load_json
from tasks import starred_releases as sr


@pytest.fixture
def stub(monkeypatch):
    releases = {}

    monkeypatch.setattr(sr, "notify_failure", lambda *a, **k: None)
    monkeypatch.setattr(sr, "fetch_latest_release", lambda full_name: releases.get(full_name, {}))
    return releases


def _repos(*names):
    return {"repos": [{"full_name": n, "name": n.split("/")[-1]} for n in names]}


def test_caches_latest_release_per_repo(stub, monkeypatch):
    stub.update({
        "a/one": {"tag": "v1.0", "name": "1.0", "published_at": "2026-07-01T00:00:00Z", "html_url": "u"},
        "b/two": {"tag": "v2.0", "name": "2.0", "published_at": "2026-07-02T00:00:00Z", "html_url": "u2"},
    })
    monkeypatch.setattr(sr, "fetch_starred_repos", lambda: _repos("a/one", "b/two"))

    assert sr.main() == 0
    store = load_json(sr.RELEASES_PATH, {})
    assert store["a/one"]["tag"] == "v1.0"
    assert store["b/two"]["tag"] == "v2.0"
    assert "checked_at" in store["a/one"]  # stamped on write


def test_repo_without_release_is_omitted(stub, monkeypatch):
    stub.update({"a/one": {"tag": "v1.0", "name": "1.0", "published_at": None, "html_url": "u"}})
    # b/two has no entry in stub -> fetch_latest_release returns {}
    monkeypatch.setattr(sr, "fetch_starred_repos", lambda: _repos("a/one", "b/two"))

    assert sr.main() == 0
    store = load_json(sr.RELEASES_PATH, {})
    assert "a/one" in store
    assert "b/two" not in store


def test_prunes_destarred_repos(stub, monkeypatch):
    atomic_write_json(sr.RELEASES_PATH, {"gone/repo": {"tag": "v0", "checked_at": "x"}})
    stub.update({"a/one": {"tag": "v1.0", "name": "1.0", "published_at": None, "html_url": "u"}})
    monkeypatch.setattr(sr, "fetch_starred_repos", lambda: _repos("a/one"))

    assert sr.main() == 0
    store = load_json(sr.RELEASES_PATH, {})
    assert "gone/repo" not in store  # whole cache rewritten from the live list
    assert "a/one" in store


def test_caches_the_repo_list_for_the_pages_fallback(stub, monkeypatch):
    # /starred falls back to this when the live GitHub fetch fails, so the page
    # renders a stale list instead of going blank (chat/routes_starred.py).
    monkeypatch.setattr(sr, "fetch_starred_repos", lambda: _repos("a/one", "b/two"))

    assert sr.main() == 0
    cached = load_json(sr.REPOS_PATH, {})
    assert [r["full_name"] for r in cached["repos"]] == ["a/one", "b/two"]
    assert "fetched_at" in cached


def test_the_repo_list_is_cached_even_if_the_release_fanout_fails(stub, monkeypatch):
    # The list is written before the per-repo release fetches, so the half that
    # is slow and rate-limit-prone can't cost the page its fallback.
    monkeypatch.setattr(sr, "fetch_starred_repos", lambda: _repos("a/one"))
    monkeypatch.setattr(sr, "fetch_latest_release",
                        lambda full_name: (_ for _ in ()).throw(RuntimeError("boom")))

    assert sr.main() == 1
    assert [r["full_name"] for r in load_json(sr.REPOS_PATH, {})["repos"]] == ["a/one"]


def test_fetch_error_notifies_and_returns_nonzero(stub, monkeypatch):
    failures = []
    monkeypatch.setattr(sr, "notify_failure", lambda name, detail, logger=None: failures.append(detail))
    monkeypatch.setattr(sr, "fetch_starred_repos", lambda: {"error": "GitHub down"})

    assert sr.main() == 1
    assert failures == ["GitHub down"]
