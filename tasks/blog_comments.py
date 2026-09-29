"""Push once a day when blog comments are waiting for approval.
Non-interactive — run by launchd daily.

The blog (~/Projects/AiBuilderBlog) keeps reader comments in Cloudflare D1, and
a new one stays hidden until it is approved at /admin/comments. Cloudflare cannot
reach the tailnet-only ntfy server, so Wren asks Cloudflare instead: one read of
the D1 REST API, with a token that can only read D1.

No "already notified" state on purpose. Every run reports the full pending
count, so a comment nobody has dealt with nags again tomorrow. Nothing pending
means no push.

The push carries counts and post slugs only. Names and comment bodies are text
from strangers and never leave D1 through here.

Usage:
    python -m tasks.blog_comments
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from agent import config
from agent.tools._http import http_error
from agent.tools.notify import notify
from tasks._common import notify_failure, setup_logger

MODERATION_URL = "https://blog.craigdube.dev/admin/comments"
QUERY = ("SELECT post_slug, count(*) AS n FROM comments "
         "WHERE status = 'pending' GROUP BY post_slug ORDER BY n DESC, post_slug")
TIMEOUT_S = 20


def fetch_pending() -> dict:
    """-> {"posts": [{"post_slug": str, "n": int}, ...]} or {"error": ...}."""
    account = config.getenv("CF_ACCOUNT_ID")
    database = config.getenv("CF_D1_DATABASE_ID")
    token = config.getenv("CF_D1_TOKEN")
    missing = [k for k, v in (("CF_ACCOUNT_ID", account), ("CF_D1_DATABASE_ID", database),
                              ("CF_D1_TOKEN", token)) if not v]
    if missing:
        return {"error": f"not set: {', '.join(missing)}"}

    url = (f"https://api.cloudflare.com/client/v4/accounts/{account}"
           f"/d1/database/{database}/query")
    try:
        resp = requests.post(url, json={"sql": QUERY},
                             headers={"Authorization": f"Bearer {token}"},
                             timeout=TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return http_error(e, phase="d1 query")

    if not data.get("success"):
        return {"error": f"D1 query failed: {data.get('errors')}"}
    try:
        return {"posts": list(data["result"][0]["results"])}
    except (KeyError, IndexError, TypeError):
        return {"error": "D1 response had no result rows"}


def summarize(posts: list) -> str:
    """'3 comments waiting (2 on a, 1 on b)'."""
    total = sum(p["n"] for p in posts)
    noun = "comment" if total == 1 else "comments"
    parts = ", ".join(f"{p['n']} on {p['post_slug']}" for p in posts)
    return f"{total} {noun} waiting ({parts})"


def main() -> int:
    logger = setup_logger("blog_comments")
    logger.info("Starting blog comments run")

    try:
        result = fetch_pending()
        if "error" in result:
            logger.error(f"fetch_pending failed: {result['error']}")
            notify_failure("blog_comments", result["error"], logger)
            return 1

        posts = result["posts"]
        if not posts:
            logger.info("No comments pending; blog comments run complete")
            return 0

        message = summarize(posts)
        sent = notify(
            message=message,
            title="Blog comments",
            actions=[{"action": "view", "label": "Moderate", "url": MODERATION_URL}],
            # Once a day and nothing retries it — notify()'s case for the fallback.
            email_fallback=True,
        )
        if "error" in sent:
            fallback = sent.get("email_fallback") or {"error": "no fallback"}
            if "error" in fallback:
                logger.error(f"Push and email both failed: {sent['error']}; {fallback['error']}")
                return 1
            logger.warning(f"Push did not send, emailed instead: {sent['error']}")
        logger.info(f"Pushed: {message}; blog comments run complete")
        return 0
    except Exception as e:
        logger.exception(f"Blog comments run failed: {e}")
        notify_failure("blog_comments", e, logger)
        return 1


if __name__ == "__main__":
    sys.exit(main())
