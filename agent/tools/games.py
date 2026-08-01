"""The registry of games Wren can play with the user, and the read-only tool
that lists them.

A game is not built into Wren — it lives in its own repo with its own rules
engine and UI, and Wren provides the front door: a `/games` page, a link that
works from the phone, and (for a game that needs a model) the same local model
chat runs. The registry below is what both surfaces read; adding a game is
adding an entry here plus its serving/proxy wiring in chat/routes_games.py.

Registry lives on the agent side rather than in chat/ because agent/ must not
import the Flask app — chat/routes_games.py imports GAMES from here, not the
other way round.

Read-only: nothing in this module writes state. `available` is a liveness
probe, not a cached flag, so a game whose service is down is listed and greyed
rather than offered as a dead link.

Usage:
    python -m agent.tools.games
"""

import json
import os
import socket
import sys
from pathlib import Path

from agent import prefs

# The user's name, for the model-facing tool description below. From
# config/preferences.json; falls back to "the user".
_NAME = prefs.user_name()

# How long to wait for a game's API service to answer. This runs on the request
# path of /games and inside a chat turn, so it has to be short enough not to be
# felt: the service is on loopback, where a live listener accepts immediately
# and a dead one refuses immediately. The timeout only bounds the pathological
# case (a listener that accepts nothing), it is not a latency budget.
PROBE_TIMEOUT_S = 0.3


def _weigh_anchor_dir() -> Path:
    """Read at call time, not import time, so tests (and a .env edit) can point
    this somewhere else without reimporting the module."""
    return Path(
        os.getenv("WEIGH_ANCHOR_DIR") or (Path.home() / "Projects" / "WeighAnchor")
    ).expanduser()


def games() -> list[dict]:
    """The registry. A function rather than a module constant so the env-derived
    paths and ports are resolved per call."""
    return [
        {
            "id": "weigh-anchor",
            "name": "Weigh Anchor",
            "blurb": (
                "A word-deduction card game. One of your five candidate cards is your "
                "anchor and only you know which; over five turns you draft cards from a "
                "shared market to point at it, then everyone reads everyone else's "
                "evidence and names an anchor."
            ),
            # Surfaced verbatim on the games card and to the model. At two seats the
            # game is cooperative by design (both bonuses switch off and you play to a
            # shared par) — saying so here beats it surprising the user mid-round.
            "players": "You plus 1-5 AI seats. At 2 seats it is cooperative: you and Wren play together against a par, not against each other.",
            "path": "/games/weigh-anchor/",
            "dist": _weigh_anchor_dir() / "dist",
            "api_port": int(os.getenv("WEIGH_ANCHOR_PORT", "3002")),
            # The AI seats run on the same local model chat uses, and Ollama serves one
            # generation at a time, so game turns and chat turns queue behind each other.
            "note": "The AI seats think with the same local model as chat, so a game turn and a chat message wait for each other.",
        },
    ]


def _service_up(port: int) -> bool:
    """True if something is listening on the loopback port. Any socket error means
    'not up' — this is a liveness hint for the UI, never a reason to raise."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


def _unavailable_reason(game: dict) -> str | None:
    """Why a game can't be played right now, phrased for the user, or None if it
    can. Order matters: an unbuilt game is the more actionable of the two."""
    if not Path(game["dist"]).is_dir():
        return "not built yet — run the build in its repo"
    if not _service_up(game["api_port"]):
        return "its model service isn't running"
    return None


def list_games() -> dict:
    """Every registered game with a link and whether it's playable right now.

    The URL is absolute when WREN_PUBLIC_URL is set, because this answer is
    routinely read on the phone, where a bare path is not tappable.
    """
    base = (os.getenv("WREN_PUBLIC_URL") or "").rstrip("/")
    out = []
    for game in games():
        reason = _unavailable_reason(game)
        out.append({
            "id": game["id"],
            "name": game["name"],
            "blurb": game["blurb"],
            "players": game["players"],
            "note": game["note"],
            "url": f"{base}{game['path']}" if base else game["path"],
            "available": reason is None,
            "unavailable_reason": reason,
        })
    return {"games": out}


TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_games",
        # The wording carries more weight than usual here. Asked the vague
        # "let's play a game" (a request to DO something, not a question about
        # what exists), the model answered from pretraining in 2 of 12 replays —
        # naming Wordle, Sudoku and Chess with invented links and calling no
        # tool. A text-only reply is shaped exactly like a legitimate one, so
        # nothing catches it. Hence the flat statement that this list is not
        # something the model knows: measured 0 of 12 after.
        "description": (
            f"List the games {_NAME} can play with you, each with a link to open it and "
            f"whether it can be played right now. Call this whenever {_NAME} asks to play "
            "anything, asks what games there are, or names a game — including a vague "
            "'let's play something'. This list is NOT something you know: only the games "
            "this tool returns exist, and their links only work if they come from it. "
            "Never name a game or a link from your own knowledge, and never guess that a "
            "game exists — if the tool returns nothing, say there are no games set up. "
            "You cannot play these in the chat itself — each has its own board that opens "
            f"in the browser, so answer with the link and let {_NAME} tap it."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def main() -> int:
    print(json.dumps(list_games(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
