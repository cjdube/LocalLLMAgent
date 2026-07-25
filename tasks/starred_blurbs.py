"""Generate and cache a one-line "what it does" blurb for each starred GitHub
repo, for the /starred view. Non-interactive — run by launchd weekly.

The blurb is written by the local model from each repo's README, one isolated
completion per repo: the model sees only that single repo's (truncated) README,
so the context window can't overflow no matter how many repos are starred — star
count drives the number of calls, not the size of any prompt. Blurbs are cached
in config/starred_blurbs.json and generated once per repo (a repo's purpose is
stable, and READMEs churn on every commit), so each run only summarizes the
newly-starred repos. Pass --refresh to regenerate every blurb.

The /starred page fetches the repo list live and merges these cached blurbs,
falling back to a repo's GitHub description for any not yet cached — so the model
never sits on the page's request path.

Usage:
    python -m tasks.starred_blurbs
    python -m tasks.starred_blurbs --refresh
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from agent.loop import complete_text, resolve_backend, warm_model
from agent.store import atomic_write_json, load_json, locked
from agent.tools.github_starred import fetch_readme, fetch_starred_repos
from tasks._common import notify_failure, setup_logger

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / "config" / ".env")

BLURBS_PATH = _ROOT / "config" / "starred_blurbs.json"

# Cap the README fed to the small model: enough to capture what the project is
# (the pitch lives up top), bounded so an enormous README can't blow the prompt.
README_CHARS = 2000

BLURB_SYSTEM_PROMPT = """You write a single short blurb saying what a GitHub \
repository does, for a table of the user's starred repos. Given the repo's README, \
write ONE plain sentence describing what the project is and does. No markdown, no \
links, no headings, no quotes around the output, no preamble — just the sentence \
itself. Be concise, concrete, and neutral."""


def _first_line(text: str) -> str:
    """The model is asked for one sentence; take the first non-empty line and
    strip it, dropping any stray preamble/blank lines defensively."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _generate_blurb(repo: dict, backend, logger) -> str:
    """One isolated completion for one repo: summarize its README, falling back
    to the repo's GitHub description when the README is missing or the model
    returns nothing usable — so every repo still gets a usable blurb."""
    readme = fetch_readme(repo["full_name"])[:README_CHARS]
    if readme.strip():
        raw = complete_text(
            system_prompt=BLURB_SYSTEM_PROMPT,
            user_prompt=f"repo: {repo['full_name']}\nREADME:\n{readme}",
            backend=backend,
            logger=logger,
            think=False,
        )
        logger.info(f"blurb {repo['full_name']} -> {raw!r}")
        blurb = _first_line(raw)
        if blurb:
            return blurb
        logger.warning(f"{repo['full_name']}: model returned nothing usable; using description")
    else:
        logger.info(f"{repo['full_name']}: no README; using description")
    return (repo.get("description") or "").strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true",
                        help="Regenerate every blurb, not just the uncached ones.")
    args = parser.parse_args(argv)

    logger = setup_logger("starred_blurbs")
    logger.info("Starting starred blurbs run")

    try:
        result = fetch_starred_repos()
        if "error" in result:
            logger.error(f"fetch_starred_repos failed: {result['error']}")
            notify_failure("starred_blurbs", result["error"], logger)
            return 1
        repos = result.get("repos", [])
        logger.info(f"{len(repos)} starred repos")

        cached = load_json(BLURBS_PATH, {})
        todo = repos if args.refresh else [r for r in repos if r["full_name"] not in cached]
        logger.info(f"{len(todo)} repos need a blurb")

        if todo:
            backend = resolve_backend("starred_blurbs")
            warm_model(logger=logger, backend=backend)
            for repo in todo:
                blurb = _generate_blurb(repo, backend, logger)
                cached[repo["full_name"]] = {
                    "blurb": blurb,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }

        # Prune de-starred repos so the store can't grow unbounded, then persist
        # under the lock the whole read-modify-write shares with any concurrent
        # reader.
        starred_names = {r["full_name"] for r in repos}
        pruned = {name: v for name, v in cached.items() if name in starred_names}
        with locked(BLURBS_PATH):
            atomic_write_json(BLURBS_PATH, pruned)
        logger.info(f"Wrote {len(pruned)} blurbs; starred blurbs run complete")
        return 0
    except Exception as e:
        logger.exception(f"Starred blurbs run failed: {e}")
        notify_failure("starred_blurbs", e, logger)
        return 1


if __name__ == "__main__":
    sys.exit(main())
