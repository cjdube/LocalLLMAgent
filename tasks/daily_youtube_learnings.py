"""Compose and write a daily YouTube-learnings review to Craig's Obsidian vault
(one file per day). Non-interactive — run by launchd every morning, covering the
prior day's Liked videos.

Craig deliberately Likes AI/tooling videos, so the day's Likes are a clean signal
for what he chose to learn. The model writes a short synthesis of what the videos
teach; a deterministic, scheme-validated linked list of the exact videos is
appended in Python. A day with no Likes writes nothing (keeps the vault clean).

Usage:
    python -m tasks.daily_youtube_learnings
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.loop import complete_text, resolve_backend, warm_model
from agent import prefs
from agent.tools.youtube import fetch_liked_videos
from tasks._common import notify_failure, setup_logger
from tasks._learnings_common import persist_or_email, prior_day, videos_section

DRAFT_SYSTEM_PROMPT = f"""You are {prefs.user_name()}'s personal executive assistant. You write a \
short thematic synthesis of the AI/technical YouTube videos he Liked yesterday — the videos he \
chose to learn from. You are running unattended — infer everything from the data given.

Use EXACTLY this template, filling in the bracketed parts (do not add extra sections, do not
include the literal brackets):

## YouTube Learnings: [Month Date, Year]

### Themes Explored
- **[Theme/Tool]:** [What this cluster of videos teaches and how it could shape future work]

Rules:
- Write **2-4 bullets total** that GROUP the videos into themes — NOT one bullet per video. \
(Every video is already listed verbatim below your synthesis, so do not re-list them.)
- Bold the theme/tool at the start of each bullet.
- Professional, analytical tone. Explain what the cluster teaches and why it matters.
- Keep it tight — a few sentences per bullet. Never repeat a word or phrase.

Output ONLY the filled-in template text, nothing else — no preamble, no explanation.
"""


def _looks_usable(text: str) -> bool:
    """The small model occasionally returns nothing, or a degenerate run that
    strip() collapses to whitespace. Treat a synthesis as usable only if it's
    non-empty and has at least one bullet."""
    return bool(text) and "- " in text


def main() -> int:
    logger = setup_logger("daily_youtube_learnings")
    logger.info("Starting daily youtube learnings run")

    try:
        start, end, day = prior_day()
        logger.info(f"Day: {day}")

        youtube_result = fetch_liked_videos(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        logger.info(f"fetch_liked_videos -> {youtube_result}")
        videos = youtube_result.get("videos", [])
        if not videos:
            # Nothing Liked yesterday — don't litter the vault with an empty file.
            # Still log the run-complete boundary: the dashboard reads status from
            # the log, and a run that starts without one is shown as "running".
            logger.info("No videos Liked yesterday; nothing to write")
            logger.info("Daily youtube learnings run complete")
            return 0

        backend = resolve_backend("daily_youtube_learnings")
        warm_model(logger=logger, backend=backend)
        # Feed the synthesis only titles + channels: the Liked-video descriptions
        # are long clickbait/link-dumps that derail the small model into repetition
        # loops, and the titles here already say what each video is about.
        titles = [{"title": v.get("title"), "channel": v.get("channel")} for v in videos]
        user_prompt = f"day: {day:%B %-d, %Y}\nyoutube_videos: {titles}\n"
        synthesis = complete_text(
            system_prompt=DRAFT_SYSTEM_PROMPT, user_prompt=user_prompt, logger=logger,
            backend=backend,
        )
        logger.info(f"Drafted synthesis:\n{synthesis}")

        # Degrade, don't emit garbage: if the small model returned nothing usable
        # (empty, or a degenerate run collapsed to whitespace by strip()), write
        # the verbatim list under a plain header rather than a broken synthesis.
        if _looks_usable(synthesis):
            body = f"{synthesis}\n\n{videos_section(videos)}\n"
        else:
            logger.warning("synthesis unusable; writing the video list without it")
            body = f"## YouTube Learnings: {day:%B %-d, %Y}\n\n{videos_section(videos)}\n"

        persist_or_email(
            body, "Daily-YouTube", day,
            subject=f"YouTube Learnings (needs manual paste) - {day:%Y-%m-%d}",
            task_name="daily_youtube_learnings", logger=logger,
        )
        logger.info("Daily youtube learnings run complete")
        return 0
    except Exception as e:
        logger.exception(f"Daily youtube learnings run failed: {e}")
        notify_failure("daily_youtube_learnings", e, logger)
        return 1


if __name__ == "__main__":
    sys.exit(main())
