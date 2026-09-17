"""Tests for the games registry and the list_games tool.

The registry reads the machine — a loopback socket probe and a build directory
on disk — so both are pinned here rather than left to the developer's state:
conftest's _isolate_games stubs _service_up off and points WEIGH_ANCHOR_DIR at
an empty tmp dir suite-wide, and these tests patch them back to exercise each
branch deliberately.
"""

import plistlib
from pathlib import Path

import pytest

from agent.tools import games as games_mod


@pytest.fixture
def built(tmp_path, monkeypatch):
    """A checkout whose bundle has been built. Returns the dist path."""
    dist = tmp_path / "built" / "dist"
    dist.mkdir(parents=True)
    monkeypatch.setenv("WEIGH_ANCHOR_DIR", str(tmp_path / "built"))
    return dist


@pytest.fixture
def service_up(monkeypatch):
    monkeypatch.setattr(games_mod, "_service_up", lambda port: True)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

def test_registry_entries_have_the_fields_both_surfaces_read():
    # The page and the tool both index these; a new game missing one would
    # render a blank card or hand the model a link it can't use.
    for game in games_mod.games():
        for field in ("id", "name", "blurb", "players", "path", "dist", "api_port", "note"):
            assert field in game, f"{game.get('id')} missing {field}"
        assert game["path"].startswith("/games/")
        assert game["path"].endswith("/")


def test_weigh_anchor_path_matches_its_registered_id():
    # The bundle is built with VITE_BASE set to this path, and routes_games
    # resolves the game by the id embedded in it. If the two drift, every asset
    # in the built bundle 404s.
    game = next(g for g in games_mod.games() if g["id"] == "weigh-anchor")
    assert game["path"] == "/games/weigh-anchor/"


def test_paths_are_read_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("WEIGH_ANCHOR_DIR", str(tmp_path / "elsewhere"))
    game = next(g for g in games_mod.games() if g["id"] == "weigh-anchor")
    assert game["dist"] == tmp_path / "elsewhere" / "dist"


def test_port_is_read_at_call_time(monkeypatch):
    monkeypatch.setenv("WEIGH_ANCHOR_PORT", "4111")
    game = next(g for g in games_mod.games() if g["id"] == "weigh-anchor")
    assert game["api_port"] == 4111


# --------------------------------------------------------------------------- #
# Availability
# --------------------------------------------------------------------------- #

def test_available_when_built_and_service_answers(built, service_up):
    game = games_mod.list_games()["games"][0]
    assert game["available"] is True
    assert game["unavailable_reason"] is None


def test_unavailable_when_not_built(service_up):
    # conftest points WEIGH_ANCHOR_DIR at a dir that doesn't exist.
    game = games_mod.list_games()["games"][0]
    assert game["available"] is False
    assert "not built" in game["unavailable_reason"]


def test_unavailable_when_service_is_down(built):
    # conftest stubs _service_up to False.
    game = games_mod.list_games()["games"][0]
    assert game["available"] is False
    assert "isn't running" in game["unavailable_reason"]


def test_not_built_is_reported_ahead_of_a_dead_service():
    # Both are wrong; building is the actionable one, and reporting "service
    # down" for an unbuilt checkout sends the user to the wrong fix.
    game = games_mod.list_games()["games"][0]
    assert "not built" in game["unavailable_reason"]


def test_service_probe_never_raises(monkeypatch):
    # The probe runs on the /games request path and inside a chat turn; a socket
    # error there must degrade to "not up", never propagate.
    def boom(*a, **k):
        raise OSError("no route to host")
    monkeypatch.setattr(games_mod.socket, "create_connection", boom)
    assert games_mod._service_up(3002) is False


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #

def test_url_is_absolute_when_public_url_is_set(monkeypatch):
    # This answer is routinely read on the phone, where a bare path isn't tappable.
    monkeypatch.setenv("WREN_PUBLIC_URL", "https://mini.ts.net")
    assert games_mod.list_games()["games"][0]["url"] == "https://mini.ts.net/games/weigh-anchor/"


def test_trailing_slash_on_public_url_does_not_double(monkeypatch):
    monkeypatch.setenv("WREN_PUBLIC_URL", "https://mini.ts.net/")
    assert games_mod.list_games()["games"][0]["url"] == "https://mini.ts.net/games/weigh-anchor/"


def test_url_falls_back_to_a_bare_path(monkeypatch):
    monkeypatch.delenv("WREN_PUBLIC_URL", raising=False)
    assert games_mod.list_games()["games"][0]["url"] == "/games/weigh-anchor/"


# --------------------------------------------------------------------------- #
# Tool schema
# --------------------------------------------------------------------------- #

def test_tool_schema_shape():
    fn = games_mod.TOOL_SCHEMA["function"]
    assert fn["name"] == "list_games"
    assert fn["parameters"]["required"] == []


def test_tool_description_tells_the_model_it_cannot_play_in_chat():
    # The small model's failure mode here is offering to play by text rather
    # than handing over the link, so the schema has to say so outright.
    assert "browser" in games_mod.TOOL_SCHEMA["function"]["description"]


def test_tool_description_forbids_naming_a_game_from_pretraining():
    # Measured, not hypothetical: asked "let's play a game" with the earlier
    # wording, the model called no tool and offered Wordle, Sudoku and Chess with
    # invented links in 2 of 12 replays. A text-only reply is shaped exactly like
    # a real one, so nothing downstream catches it — the description saying the
    # list is not something the model knows is the fix, and it took the replay to
    # 12 of 12. Pinned here so it survives a future trim of the wording.
    description = games_mod.TOOL_SCHEMA["function"]["description"]
    assert "NOT something you know" in description
    assert "Never name a game or a link from your own knowledge" in description


# --------------------------------------------------------------------------- #
# The committed service plists
# --------------------------------------------------------------------------- #
# A game's service plist and its registry entry carry the same port in two
# places, and the plist's ProgramArguments decide whether launchd's KeepAlive
# can see the service die at all. Both are silent when wrong: the game just
# greys out on /games, which reads as "I forgot to start it".

_PLIST_DIR = Path(__file__).resolve().parent.parent / "launchd" / "infra"
# (registry id, plist filename). A game with no plist is not listed here.
_GAME_PLISTS = [
    ("weigh-anchor", "local.wren.weighanchor.plist"),
    ("train-game", "local.wren.traingame.plist"),
]


@pytest.mark.parametrize("game_id,filename", _GAME_PLISTS)
def test_service_plist_port_matches_the_registry(game_id, filename, monkeypatch):
    path = _PLIST_DIR / filename
    assert path.is_file(), f"{filename} is missing; update _GAME_PLISTS too"
    plist = plistlib.loads(path.read_bytes())

    # The registry resolves its port from the environment, and conftest may have
    # set one. Read the schema default, which is what the plist has to match.
    monkeypatch.delenv("WEIGH_ANCHOR_PORT", raising=False)
    monkeypatch.delenv("TRAIN_GAME_PORT", raising=False)
    game = next(g for g in games_mod.games() if g["id"] == game_id)

    assert plist["EnvironmentVariables"]["PORT"] == str(game["api_port"]), (
        f"{filename} starts the service on a different port than the proxy dials"
    )


@pytest.mark.parametrize("game_id,filename", _GAME_PLISTS)
def test_service_plist_runs_the_server_not_a_wrapper(game_id, filename):
    """launchd watches the process it spawns. tsx/dist/cli.mjs spawns node as a
    CHILD and waits, so the job stays "running" after the server dies and
    KeepAlive never fires.

    Both games shipped this bug. Weigh Anchor ran that way for 19 days —
    launchd tracked pid 1438 while the server answering on 3002 was pid 1841 —
    and it survived only because the server never crashed. Loading tsx's hooks
    into node directly keeps the watched pid and the listening pid the same;
    measured 2026-09-17, both games then recovered from a kill -9 in seconds.

    Parameterized rather than written per game on purpose: the next game's plist
    will be copied from one of these, and the copy is how the bug spread."""
    plist = plistlib.loads((_PLIST_DIR / filename).read_bytes())
    argv = plist["ProgramArguments"]
    assert not any("cli.mjs" in arg for arg in argv), (
        f"{filename}: running the tsx CLI makes launchd watch a wrapper, not the server"
    )
    assert plist["KeepAlive"] is True, filename
