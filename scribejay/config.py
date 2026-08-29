"""Settings seam for ScribeJay: every scribejay/*.py module reads env vars and
preferences through here, never by calling os.getenv or agent.prefs directly.

This is the seam the future setup wizard plugs into: Phase 2 of the ScribeJay
split (docs/reviews/scribejay-split-plan.md) swaps this module's backing store
from `.env` to `~/.scribejay/config.toml`, and nothing else moves.
"""

import os

from agent import prefs


def getenv(key: str, default=None):
    return os.getenv(key, default)


def user_name() -> str:
    return prefs.user_name()


def calendar_categories() -> list:
    return prefs.calendar_categories()


def category_color_by_role(role: str, default: str) -> str:
    return prefs.category_color_by_role(role, default)
