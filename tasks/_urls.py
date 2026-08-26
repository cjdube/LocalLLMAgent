"""URL guards shared by everything that renders an externally-sourced link.

One function, deliberately: this is the scheme allow-list that stands between a
feed/API/model-supplied URL and the HTML or Markdown a digest emails out, and it
used to exist as three identical copies (tasks/morning_brief.py,
tasks/opportunity_digest.py, agent/activity_log.py) under two different
names. Nothing had drifted yet, which was the reason to consolidate: a future
hardening would have landed in one copy and quietly left the other two emitters
unpatched, and a grep for the private name found only two thirds of the call
sites.

Import `safe_url` from here rather than copying it.
"""

from urllib.parse import urlparse


def safe_url(url: str) -> str:
    """Return url only if it's an http(s) link, else "". Guards against
    javascript:/data: (or other) schemes in externally-sourced URLs —
    html.escape() alone does not neutralize a dangerous scheme, because the
    danger is in the scheme rather than in any character escaping would touch.

    Returning "" rather than raising is what lets a caller degrade to unlinked
    text: a bad URL costs its link, not the whole digest."""
    try:
        return url if urlparse(url).scheme in ("http", "https") else ""
    except (ValueError, AttributeError):
        return ""
