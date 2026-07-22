"""Cache the latest published release for each starred GitHub repo, for the
/starred view's release-awareness column. Non-interactive — run by launchd daily.

For each starred repo it fetches the latest release (best-effort, releases only —
a repo that doesn't publish releases simply gets no version), keyed by full_name
in config/starred_releases.json. The /starred page reads this cache and merges it
into the live repo list, so no release fan-out happens on the page's request path.

Unlike the blurbs job, every repo is refreshed each run — a repo's blurb is stable
but its latest release changes, so there's no "only uncached" skip. De-starred
repos are pruned on write so the store can't grow unbounded.

Usage:
    python -m tasks.starred_releases
"""

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from agent.store import atomic_write_json, locked
from agent.tools.github_starred import fetch_latest_release, fetch_starred_repos
from tasks._common import notify_failure, setup_logger

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / "config" / ".env")

RELEASES_PATH = _ROOT / "config" / "starred_releases.json"

# fetch_latest_release makes one HTTP round-trip per repo and never raises, so a
# small pool cuts wall-clock without changing behaviour — same courtesy to
# GitHub's rate limiter as fetch_starred_repos' enrich loop.
MAX_WORKERS = 5


def main() -> int:
    logger = setup_logger("starred_releases")
    logger.info("Starting starred releases run")

    try:
        result = fetch_starred_repos()
        if "error" in result:
            logger.error(f"fetch_starred_repos failed: {result['error']}")
            notify_failure("starred_releases", result["error"], logger)
            return 1
        repos = result.get("repos", [])
        logger.info(f"{len(repos)} starred repos")

        checked_at = datetime.now(timezone.utc).isoformat()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            releases = pool.map(lambda r: fetch_latest_release(r["full_name"]), repos)

        cache = {}
        for repo, release in zip(repos, releases):
            if release:
                cache[repo["full_name"]] = {**release, "checked_at": checked_at}

        with locked(RELEASES_PATH):
            atomic_write_json(RELEASES_PATH, cache)
        logger.info(f"Wrote {len(cache)} releases; starred releases run complete")
        return 0
    except Exception as e:
        logger.exception(f"Starred releases run failed: {e}")
        notify_failure("starred_releases", e, logger)
        return 1


if __name__ == "__main__":
    sys.exit(main())
