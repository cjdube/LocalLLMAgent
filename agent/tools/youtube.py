"""Fetch videos the user Liked on YouTube within a date range.

The user habitually Likes AI/tooling videos on their brand channel, which makes
the Likes playlist a clean, intentional signal for the
weekly learnings review — stronger than raw browsing history, which only shows
what was loaded, not what was engaged with. The watch-history playlist has been
non-functional through the API since ~2016, but the Likes playlist works.

Library module, not a registered chat tool. Wren stopped carrying the capture
tools when journaling moved to ScribeJay (scribejay/__init__.py): the callers now are
ScribeJay's daily entries and Wren's tasks/daily_synthesis.py, which import the
function directly. There is deliberately no TOOL_SCHEMA here — if you add one
back, register it in agent/toolset.py or tests/test_toolset.py will fail.

Uses the shared Google OAuth helper (agent/tools/google_auth.py). This needs the
`youtube.readonly` scope — add it to that module's SCOPES, delete
config/google_token.json, and re-run `python -m agent.tools.google_auth`,
selecting the brand channel at the picker (the API's mine=True operates on
the channel chosen during consent).

Usage:
    python -m agent.tools.youtube --start 2026-06-30 --end 2026-07-06
"""

import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from agent.dates import local_timezone, resolve_date
from agent.tools._http import load_env, print_result
from agent.tools.google_auth import build_service

load_env()

def _likes_playlist_id() -> str:
    """The authenticated channel's Likes playlist id. mine=True resolves to the
    channel selected during OAuth consent (the brand channel)."""
    service = build_service("youtube", "v3")
    result = service.channels().list(part="contentDetails", mine=True).execute()
    items = result.get("items", [])
    if not items:
        raise RuntimeError("no YouTube channel on the authorized account")
    return items[0]["contentDetails"]["relatedPlaylists"]["likes"]


def _video_from_item(item: dict) -> dict:
    """Flatten a playlistItems entry into the fields we care about. In the Likes
    playlist, snippet.publishedAt is when the video was *added* (i.e. Liked)."""
    snippet = item.get("snippet", {})
    video_id = item.get("contentDetails", {}).get("videoId", "")
    return {
        "video_id": video_id,
        "title": snippet.get("title", ""),
        "channel": snippet.get("videoOwnerChannelTitle", ""),
        "description": snippet.get("description", ""),
        "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else "",
        "liked_at": snippet.get("publishedAt", ""),
    }


def _liked_local_date(published_at: str) -> str:
    """The local calendar date a video was Liked on, from the UTC publishedAt.

    The API stamps like-times in UTC but the user's day boundaries are local, so a
    video Liked at 9pm EDT carries the *next* UTC date. Comparing the raw UTC
    date against a local day window silently drops every evening Like. Returns
    "" for an unparseable stamp, which no window matches."""
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return dt.astimezone(ZoneInfo(local_timezone())).date().isoformat()


def fetch_liked_videos(start_date: str, end_date: str) -> dict:
    """Liked videos whose like-date falls within [start_date, end_date] inclusive.

    Dates are resolved in Python (never trusting the model to compute them);
    explicit 'YYYY-MM-DD' strings — what scribejay.daily_youtube_learnings passes — are
    honored as-is. Returns {"videos": [...]} or, on any failure, the same shape
    with an "error" so callers can degrade to an empty list.
    """
    start = resolve_date(start_date)
    end = resolve_date(end_date)

    try:
        service = build_service("youtube", "v3")
        playlist_id = _likes_playlist_id()

        videos, page_token = [], None
        while True:
            result = (
                service.playlistItems()
                .list(part="snippet,contentDetails", playlistId=playlist_id, maxResults=50, pageToken=page_token)
                .execute()
            )
            items = result.get("items", [])
            # The Likes playlist is ordered most-recently-liked first, so once
            # an item predates the window every remaining item does too — stop
            # paginating rather than burning quota on the whole history.
            stop = False
            for item in items:
                liked_date = _liked_local_date(item.get("snippet", {}).get("publishedAt", ""))
                if liked_date and liked_date < start:
                    stop = True
                    break
                if start <= liked_date <= end:
                    videos.append(_video_from_item(item))

            page_token = result.get("nextPageToken")
            if stop or not page_token:
                break
    except Exception as e:
        return {"start_date": start, "end_date": end, "video_count": 0, "videos": [], "error": str(e)}

    return {"start_date": start, "end_date": end, "video_count": len(videos), "videos": videos}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="First day of the range (YYYY-MM-DD).")
    parser.add_argument("--end", required=True, help="Last day of the range, inclusive (YYYY-MM-DD).")
    args = parser.parse_args()

    result = fetch_liked_videos(args.start, args.end)
    return print_result(result)


if __name__ == "__main__":
    sys.exit(main())
