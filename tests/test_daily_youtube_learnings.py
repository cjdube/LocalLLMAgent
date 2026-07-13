"""Tests for tasks/daily_youtube_learnings.py — main() drafts a synthesis, appends
the deterministic video list, and persists a Daily-YouTube entry; a day with no
Liked videos writes nothing. Collaborators are monkeypatched; no model, YouTube,
vault, or Gmail access."""

import pytest

from tasks import daily_youtube_learnings as yt


@pytest.fixture
def stubbed_run(monkeypatch):
    seen = {"persists": []}
    monkeypatch.setattr(yt, "resolve_backend", lambda key: None)
    monkeypatch.setattr(yt, "warm_model", lambda **k: True)
    monkeypatch.setattr(yt, "complete_text",
                        lambda **k: "## YouTube Learnings: July 12, 2026\n\n### Themes Explored\n- **Git:** internals")
    monkeypatch.setattr(yt, "persist_or_email",
                        lambda content, prefix, day, subject, task_name, logger:
                        seen["persists"].append((prefix, content)) or {"written": True})
    monkeypatch.setattr(yt, "notify_failure", lambda *a, **k: None)
    return seen


def test_happy_path_persists_synthesis_and_video_list(stubbed_run, monkeypatch):
    monkeypatch.setattr(yt, "fetch_liked_videos", lambda *a, **k: {"videos": [
        {"title": "Git Deep Dive", "channel": "LearnThatStack",
         "url": "https://www.youtube.com/watch?v=abc", "description": "how git works"},
    ]})
    assert yt.main() == 0
    assert len(stubbed_run["persists"]) == 1
    prefix, content = stubbed_run["persists"][0]
    assert prefix == "Daily-YouTube"
    # model synthesis + the deterministic linked list are both present
    assert "### Themes Explored" in content
    assert "### Videos Liked" in content
    assert "https://www.youtube.com/watch?v=abc" in content


def test_unusable_synthesis_degrades_to_list(stubbed_run, monkeypatch):
    # a degenerate/empty model reply must not write a broken file — fall back to a
    # plain header + the deterministic video list.
    monkeypatch.setattr(yt, "complete_text", lambda **k: "   \n\n")
    monkeypatch.setattr(yt, "fetch_liked_videos", lambda *a, **k: {"videos": [
        {"title": "Git Deep Dive", "channel": "LTS", "url": "https://youtu.be/abc"},
    ]})
    assert yt.main() == 0
    _, content = stubbed_run["persists"][0]
    assert content.startswith("## YouTube Learnings:")
    assert "### Themes Explored" not in content  # synthesis dropped
    assert "### Videos Liked" in content         # list still written


def test_no_videos_writes_nothing(stubbed_run, monkeypatch):
    monkeypatch.setattr(yt, "fetch_liked_videos", lambda *a, **k: {"videos": []})
    assert yt.main() == 0
    assert stubbed_run["persists"] == []  # skipped the empty day


def test_fetch_failure_is_a_failed_run(stubbed_run, monkeypatch):
    calls = []
    monkeypatch.setattr(yt, "fetch_liked_videos",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api boom")))
    monkeypatch.setattr(yt, "notify_failure", lambda name, detail, logger=None: calls.append(str(detail)))
    assert yt.main() == 1
    assert any("api boom" in c for c in calls)
