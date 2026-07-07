"""Search the web via the Tavily API.

Usage:
    python -m agent.tools.web_search --query "latest NH state park alerts"
    python -m agent.tools.web_search --query "who won the game last night" --topic news
    python -m agent.tools.web_search --query "how does WireGuard handshake work" --max-results 3

Key resolution order: --api-key arg > config/.env file > TAVILY_API_KEY env var
"""

import argparse
import sys

import requests

from agent.tools._http import http_error, load_env, missing_key_error, print_result, resolve_key

load_env()

SEARCH_URL = "https://api.tavily.com/search"

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_web",
        "description": (
            "Search the web for current information — news, facts, prices, "
            "documentation, anything not in the model's training data or "
            "Craig's local tools. Returns a short list of relevant results "
            "with titles, URLs, and content snippets."
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
                        "For topic='news' only: limit results to those published "
                        "within the last N days. Use a small value (e.g. 1) when "
                        "you need genuinely current headlines rather than "
                        "whatever ranks highest."
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


def _parse(raw: dict) -> dict:
    results = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", ""),
            # Kept so callers can see/sort by recency — Tavily returns this for
            # news results; without it we can't tell fresh headlines from stale.
            "published_date": r.get("published_date", ""),
        }
        for r in raw.get("results", [])
    ]
    out = {"results": results}
    answer = raw.get("answer")
    if answer:
        out["answer"] = answer
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
