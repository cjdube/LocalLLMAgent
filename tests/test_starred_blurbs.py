"""Tests for tasks/starred_blurbs.py — main() summarizes each uncached repo's
README into a one-line blurb, caches it, skips already-cached repos, prunes
de-starred entries, and falls back to the GitHub description when a README is
missing. Collaborators are monkeypatched; no model or GitHub access. The blurb
store is redirected to tmp_path by conftest."""

import pytest

from agent.store import load_json
from tasks import starred_blurbs as sb


@pytest.fixture
def stub(monkeypatch):
    calls = {"complete": [], "readmes": {}}

    monkeypatch.setattr(sb, "resolve_backend", lambda key: None)
    monkeypatch.setattr(sb, "warm_model", lambda **k: True)
    monkeypatch.setattr(sb, "notify_failure", lambda *a, **k: None)

    def _fetch_readme(full_name, api_key=None):
        return calls["readmes"].get(full_name, "")
    monkeypatch.setattr(sb, "fetch_readme", _fetch_readme)

    def _complete_text(system_prompt, user_prompt, **k):
        calls["complete"].append(user_prompt)
        return "A tool that does the thing.\n"
    monkeypatch.setattr(sb, "complete_text", _complete_text)
    return calls


def _repos(*names_desc):
    return {"repos": [{"full_name": n, "name": n.split("/")[-1], "description": d}
                      for n, d in names_desc]}


def test_generates_blurb_from_readme_for_each_repo(stub, monkeypatch):
    stub["readmes"] = {"a/one": "# One\nDoes one thing.", "b/two": "# Two\nDoes two things."}
    monkeypatch.setattr(sb, "fetch_starred_repos",
                        lambda: _repos(("a/one", "desc one"), ("b/two", "desc two")))

    assert sb.main([]) == 0
    store = load_json(sb.BLURBS_PATH, {})
    assert store["a/one"]["blurb"] == "A tool that does the thing."
    assert store["b/two"]["blurb"] == "A tool that does the thing."
    assert len(stub["complete"]) == 2  # one isolated call per repo


def test_skips_already_cached_repos(stub, monkeypatch):
    from agent.store import atomic_write_json
    atomic_write_json(sb.BLURBS_PATH, {"a/one": {"blurb": "cached", "generated_at": "x"}})
    stub["readmes"] = {"a/one": "readme", "b/two": "readme"}
    monkeypatch.setattr(sb, "fetch_starred_repos",
                        lambda: _repos(("a/one", "d1"), ("b/two", "d2")))

    assert sb.main([]) == 0
    store = load_json(sb.BLURBS_PATH, {})
    assert store["a/one"]["blurb"] == "cached"          # untouched
    assert store["b/two"]["blurb"] == "A tool that does the thing."
    assert len(stub["complete"]) == 1                   # only the new repo hit the model


def test_refresh_regenerates_all(stub, monkeypatch):
    from agent.store import atomic_write_json
    atomic_write_json(sb.BLURBS_PATH, {"a/one": {"blurb": "cached", "generated_at": "x"}})
    stub["readmes"] = {"a/one": "readme"}
    monkeypatch.setattr(sb, "fetch_starred_repos", lambda: _repos(("a/one", "d1")))

    assert sb.main(["--refresh"]) == 0
    store = load_json(sb.BLURBS_PATH, {})
    assert store["a/one"]["blurb"] == "A tool that does the thing."  # overwritten
    assert len(stub["complete"]) == 1


def test_prunes_destarred_repos(stub, monkeypatch):
    from agent.store import atomic_write_json
    atomic_write_json(sb.BLURBS_PATH, {"gone/repo": {"blurb": "old", "generated_at": "x"}})
    stub["readmes"] = {"a/one": "readme"}
    monkeypatch.setattr(sb, "fetch_starred_repos", lambda: _repos(("a/one", "d1")))

    assert sb.main([]) == 0
    store = load_json(sb.BLURBS_PATH, {})
    assert "gone/repo" not in store   # no longer starred → pruned
    assert "a/one" in store


def test_missing_readme_falls_back_to_description(stub, monkeypatch):
    stub["readmes"] = {}  # fetch_readme returns "" for every repo
    monkeypatch.setattr(sb, "fetch_starred_repos", lambda: _repos(("a/one", "the description")))

    assert sb.main([]) == 0
    store = load_json(sb.BLURBS_PATH, {})
    assert store["a/one"]["blurb"] == "the description"
    assert stub["complete"] == []   # no README → model never called


def test_unusable_model_output_falls_back_to_description(stub, monkeypatch):
    stub["readmes"] = {"a/one": "a readme"}
    monkeypatch.setattr(sb, "complete_text", lambda **k: "   \n\n")  # degenerate
    monkeypatch.setattr(sb, "fetch_starred_repos", lambda: _repos(("a/one", "fallback desc")))

    assert sb.main([]) == 0
    store = load_json(sb.BLURBS_PATH, {})
    assert store["a/one"]["blurb"] == "fallback desc"


def test_fetch_error_notifies_and_returns_nonzero(stub, monkeypatch):
    failures = []
    monkeypatch.setattr(sb, "notify_failure", lambda name, detail, logger=None: failures.append(detail))
    monkeypatch.setattr(sb, "fetch_starred_repos", lambda: {"error": "GitHub down"})

    assert sb.main([]) == 1
    assert failures == ["GitHub down"]
