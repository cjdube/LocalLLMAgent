"""Tests for agent.tools.youtube and the weekly-learnings video compaction.

The live YouTube API is faked with a small stand-in service (matching the
project's precedent of not hitting live Google APIs in tests), which lets us
exercise the real logic worth testing: the [start, end] date window, the
stop-paginating-once-past-the-window shortcut, and graceful error degradation.
The pure helpers (_video_from_item, compact_videos) are unit-tested directly.
"""

import agent.tools.youtube as youtube
from agent.tools.youtube import _video_from_item, fetch_liked_videos
from agent.activity_log import (
    MAX_YOUTUBE_DESC_CHARS,
    MAX_YOUTUBE_VIDEOS,
    compact_videos,
)


# --------------------------------------------------------------------------- #
# Fake YouTube service
# --------------------------------------------------------------------------- #

class _FakeRequest:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeChannels:
    def __init__(self, likes_id):
        self._likes_id = likes_id

    def list(self, **kwargs):
        return _FakeRequest(
            {"items": [{"contentDetails": {"relatedPlaylists": {"likes": self._likes_id}}}]}
        )


class _FakePlaylistItems:
    def __init__(self, pages):
        self._pages = pages  # {pageToken (None for first): result dict}

    def list(self, **kwargs):
        # KeyError if the code requests a page we didn't provide — used to
        # assert the stop-early shortcut never fetches the next page.
        return _FakeRequest(self._pages[kwargs.get("pageToken")])


class _FakeService:
    def __init__(self, likes_id, pages):
        self._channels = _FakeChannels(likes_id)
        self._items = _FakePlaylistItems(pages)

    def channels(self):
        return self._channels

    def playlistItems(self):
        return self._items


def _item(video_id, published_at, title="T", channel="Chan", description="desc"):
    return {
        "contentDetails": {"videoId": video_id},
        "snippet": {
            "title": title,
            "videoOwnerChannelTitle": channel,
            "description": description,
            "publishedAt": published_at,
        },
    }


def _patch_service(monkeypatch, pages, likes_id="LL123"):
    fake = _FakeService(likes_id, pages)
    monkeypatch.setattr(youtube, "build_service", lambda *a, **k: fake)


# --------------------------------------------------------------------------- #
# _video_from_item
# --------------------------------------------------------------------------- #

def test_video_from_item_flattens_fields():
    v = _video_from_item(_item("abc123", "2026-07-05T14:00:00Z", title="LangGraph", channel="AI Chan"))
    assert v["video_id"] == "abc123"
    assert v["title"] == "LangGraph"
    assert v["channel"] == "AI Chan"
    assert v["url"] == "https://www.youtube.com/watch?v=abc123"
    assert v["liked_at"] == "2026-07-05T14:00:00Z"


def test_video_from_item_missing_video_id_gives_empty_url():
    v = _video_from_item({"snippet": {"title": "Deleted video"}})
    assert v["video_id"] == ""
    assert v["url"] == ""


# --------------------------------------------------------------------------- #
# fetch_liked_videos — date window
# --------------------------------------------------------------------------- #

def test_fetch_filters_to_window(monkeypatch):
    pages = {
        None: {
            "items": [
                _item("A", "2026-07-08T00:00:00Z"),  # after end -> excluded
                _item("B", "2026-07-05T00:00:00Z"),  # in window
                _item("C", "2026-07-01T00:00:00Z"),  # in window
                _item("D", "2026-06-28T00:00:00Z"),  # before start -> stop
            ]
        }
    }
    _patch_service(monkeypatch, pages)

    result = fetch_liked_videos("2026-06-30", "2026-07-06")

    assert [v["video_id"] for v in result["videos"]] == ["B", "C"]
    assert result["video_count"] == 2


def test_fetch_stops_paginating_once_past_window(monkeypatch):
    # An old item on page 2 must halt pagination before page 3 is ever
    # requested — page 3 is absent, so fetching it would raise KeyError.
    pages = {
        None: {"items": [_item("B", "2026-07-05T00:00:00Z")], "nextPageToken": "p2"},
        "p2": {"items": [_item("D", "2026-06-28T00:00:00Z")], "nextPageToken": "p3"},
    }
    _patch_service(monkeypatch, pages)

    result = fetch_liked_videos("2026-06-30", "2026-07-06")

    assert [v["video_id"] for v in result["videos"]] == ["B"]


def test_fetch_windows_on_local_date_not_utc(monkeypatch):
    # publishedAt is UTC; an evening Like carries the next UTC date. Liking at
    # 9:20pm EDT on Jul 13 stamps 2026-07-14T01:20Z, and windowing on the raw UTC
    # date dropped it from the Jul 13 run entirely.
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    pages = {
        None: {
            "items": [
                _item("EVENING", "2026-07-14T01:20:00Z"),  # 9:20pm Jul 13 local
                _item("MORNING", "2026-07-13T13:00:00Z"),  # 9:00am Jul 13 local
                _item("PRIOR", "2026-07-13T01:00:00Z"),    # 9:00pm Jul 12 local -> stop
            ]
        }
    }
    _patch_service(monkeypatch, pages)

    result = fetch_liked_videos("2026-07-13", "2026-07-13")

    assert [v["video_id"] for v in result["videos"]] == ["EVENING", "MORNING"]


def test_fetch_error_degrades_to_empty_list(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("quota exceeded")

    monkeypatch.setattr(youtube, "build_service", boom)

    result = fetch_liked_videos("2026-06-30", "2026-07-06")

    assert result["videos"] == []
    assert "quota exceeded" in result["error"]


# --------------------------------------------------------------------------- #
# compact_videos (agent.activity_log)
# --------------------------------------------------------------------------- #

def test_compact_videos_caps_count():
    videos = [{"title": f"v{i}", "channel": "c", "description": "d", "url": "u"} for i in range(MAX_YOUTUBE_VIDEOS + 5)]
    assert len(compact_videos(videos)) == MAX_YOUTUBE_VIDEOS


def test_compact_videos_truncates_description():
    videos = [{"title": "t", "channel": "c", "description": "x" * (MAX_YOUTUBE_DESC_CHARS + 100), "url": "u"}]
    assert len(compact_videos(videos)[0]["description"]) == MAX_YOUTUBE_DESC_CHARS


def test_compact_videos_keeps_only_expected_fields():
    videos = [{"title": "t", "channel": "c", "description": "d", "url": "u", "video_id": "x", "liked_at": "z"}]
    assert set(compact_videos(videos)[0]) == {"title", "channel", "description", "url"}
