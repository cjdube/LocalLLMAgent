"""Search the web via the Tavily API.

Usage:
    python -m agent.tools.web_search --query "latest NH state park alerts"
    python -m agent.tools.web_search --query "who won the game last night" --topic news
    python -m agent.tools.web_search --query "how does WireGuard handshake work" --max-results 3

Key resolution order: --api-key arg > config/.env file > TAVILY_API_KEY env var
"""

import argparse
import json
import sys

import requests

from agent import prefs
from agent.tools._http import http_error, load_env, missing_key_error, print_result, resolve_key

load_env()

# The user's name, for the model-facing tool descriptions below. From
# config/preferences.json; falls back to "the user".
_NAME = prefs.user_name()

SEARCH_URL = "https://api.tavily.com/search"

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_web",
        "description": (
            "Search the web for current information — anything not in your "
            f"training data or {_NAME}'s local tools. Returns results with "
            "titles, URLs, and content snippets."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query.",
                },
                "topic": {
                    "type": "string",
                    "enum": ["general", "news"],
                    "description": "'news' biases toward recent news coverage; default 'general'.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of results to return (default 5, max 10).",
                },
                "days": {
                    "type": "integer",
                    "description": (
                        "For topic='news' only: only results published in the "
                        "last N days. Use a small value (e.g. 1) for genuinely "
                        "current headlines."
                    ),
                },
            },
            "required": ["query"],
        },
    },
}


def _search(query: str, topic: str, max_results: int, api_key: str, days: int = None) -> dict:
    payload = {
        "api_key": api_key,
        "query": query,
        "topic": topic,
        "max_results": max_results,
        "include_answer": True,
    }
    # Tavily only honors `days` for the news topic; omit it otherwise so a
    # general search isn't silently constrained.
    if days is not None and topic == "news":
        payload["days"] = days
    resp = requests.post(SEARCH_URL, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


# Size budgets. max_results bounds the result COUNT, never the size of one, so
# a page of scrapings was unbounded — the shape that made the agent loop's blind
# backstop fire on an ordinary 5-result news search. Every field Tavily fills is
# remote text of arbitrary length (content, answer, even title and url), so a
# per-field cut alone can't bound the payload; MAX_PAYLOAD_CHARS is what
# actually holds it, and the per-field caps keep any one result from eating the
# whole budget. MAX_CONTENT_CHARS stays well above research.py's _SNIPPET_CHARS
# (400), so that caller sees no change.
MAX_CONTENT_CHARS = 1200
MAX_ANSWER_CHARS = 1500
MAX_PAYLOAD_CHARS = 12000


def _trim(text: str, cap: int) -> str:
    text = text or ""
    return text if len(text) <= cap else text[:cap] + " ... [trimmed]"


def _parse(raw: dict) -> dict:
    # `answer` leads: it's Tavily's direct reply to the query, the field the
    # model actually answers from, so it's the one thing that must survive if
    # anything downstream trims this result. It used to come last, and a 240-char
    # overrun cut it off mid-sentence while five full page scrapings sat in front
    # of it. Same reasoning as push_log's summary-first ordering.
    out = {}
    answer = raw.get("answer")
    if answer:
        out["answer"] = _trim(answer, MAX_ANSWER_CHARS)
    out["results"] = []

    # Take whole results until the budget is spent, rather than letting the
    # backstop slice the JSON mid-string. A dropped result is reported, so the
    # model knows the list is partial instead of reading it as everything there
    # was — a trimmed result looks complete from the inside.
    incoming = raw.get("results", [])
    kept = []
    for r in incoming:
        kept.append({
            "title": _trim(r.get("title", ""), 300),
            "url": _trim(r.get("url", ""), 500),
            "content": _trim(r.get("content", ""), MAX_CONTENT_CHARS),
            # Kept so callers can see/sort by recency — Tavily returns this for
            # news results; without it we can't tell fresh headlines from stale.
            "published_date": _trim(r.get("published_date", ""), 100),
        })
        out["results"] = kept
        if len(json.dumps(out)) > MAX_PAYLOAD_CHARS:
            kept.pop()
            out["results"] = kept
            break

    dropped = len(incoming) - len(kept)
    if dropped:
        out["results_omitted"] = (
            f"{dropped} more result(s) were dropped to fit the context window. "
            f"Showing the top {len(kept)} of {len(incoming)}."
        )
    return out


def search_web(
    query: str,
    topic: str = "general",
    max_results: int = 5,
    api_key: str = None,
    days: int = None,
) -> dict:
    """Callable entrypoint used by the agent loop's tool dispatcher."""
    api_key = resolve_key("TAVILY_API_KEY", api_key)
    if not api_key:
        return missing_key_error("TAVILY_API_KEY")

    if not query or not query.strip():
        return {"error": "query must not be empty"}

    topic = topic if topic in ("general", "news") else "general"
    max_results = max(1, min(int(max_results or 5), 10))
    days = max(1, int(days)) if days is not None else None

    try:
        raw = _search(query.strip(), topic, max_results, api_key, days=days)
    except Exception as e:
        return http_error(e)

    try:
        return _parse(raw)
    except Exception as e:
        return {"error": f"parse error: {e}"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True)
    parser.add_argument("--topic", default="general", choices=["general", "news"])
    parser.add_argument("--max-results", dest="max_results", type=int, default=5)
    parser.add_argument("--days", dest="days", type=int, default=None,
                        help="News only: restrict to the last N days.")
    parser.add_argument("--api-key", dest="api_key", default=None)
    args = parser.parse_args()

    result = search_web(args.query, args.topic, args.max_results, args.api_key, days=args.days)
    return print_result(result)


if __name__ == "__main__":
    sys.exit(main())
