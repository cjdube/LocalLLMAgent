# Games

Wren hosts games you play *with* her — a `/games` page listing what's playable,
and a `list_games` chat tool so asking "what can we play?" answers with a link.

Wren doesn't implement any game. Each one lives in its own repo with its own
rules engine and UI; Wren provides the front door, and — for a game whose
opponents need a model — the local model those opponents think with.

Currently one game is registered: **Weigh Anchor**
(`~/Projects/WeighAnchor`), a word-deduction card game.

## How a hosted game fits together

```
phone ──HTTPS──> tailscale serve ──> Wren :8420 ─┬─ /games            the list
                                                 ├─ /games/<id>/…     the built bundle, off disk
                                                 └─ /games/<id>/api/ai/…
                                                            │ proxy, loopback only
                                                            ▼
                                                   game service :3002
                                                            │
                                                            ▼
                                                      Ollama :11434
```

Two properties of that shape are deliberate:

- **The game is mounted under Wren's origin, not given a port of its own.** The
  game has no authentication — it expects to sit behind something that does. Wren's
  token login and the single `tailscale serve` front door are that something.
  Publishing the game service on its own tailnet port would be a way in that skips
  the token, which is why `server/index.ts` binds `127.0.0.1` by default.
- **The proxy is dumb.** It forwards the body and hands back the response. Prompt
  construction, schema validation and fallbacks all belong to the game's own
  service; a second copy here would be a second thing to drift.

## The two constraints worth knowing before you play

**Game turns and chat turns queue behind each other.** The AI seats think with
the same local model chat uses, and Ollama serves one generation at a time. A
message sent to Wren mid-game waits for the current seat to finish, and vice
versa. This is a latency surprise, not a bug — both sides hold the model with
`keep_alive` precisely so neither pays a reload.

**At two seats, Weigh Anchor is cooperative.** This is the game's design, not a
limitation of playing against Wren: with only two players, the player who built
your row is also the only one who will ever read it, which removes both scoring
pressures. So the two-player variant switches off both bonuses, pools the score,
and plays against a par. Playing "against Wren" at 1v1 is really playing *with*
her. The full argument is in that repo's README.

## Deploying Weigh Anchor

The bundle Wren serves is a build artifact of the other repo, so a change to the
game needs a rebuild before it shows up. This is not automated on purpose —
Wren shouldn't be running another repo's build.

```bash
cd ~/Projects/WeighAnchor
VITE_BASE=/games/weigh-anchor/ npm run build
```

`VITE_BASE` matters: every in-app URL derives from `import.meta.env.BASE_URL`, so
a bundle built without it requests its assets and its AI calls from `/`, and every
one of them 404s. Build it plain (`npm run build`) only for serving at a root.

Then install the model service, once:

```bash
cd ~/Projects/LocalLLMAgent
./launchd/install.sh launchd/infra/local.wren.weighanchor.plist
```

It lives in `launchd/infra/` rather than `launchd/` because
`chat/insights.py:discover_tasks` globs `launchd/*.plist` non-recursively —
a directory down keeps another repo's service off Wren's dashboard, same as
colima. `install.sh` skips the directory unless you name the file.

To hack on the game locally afterwards, boot the service out first — it holds
port 3002, which is also what `npm run dev` wants:

```bash
launchctl bootout gui/$(id -u)/local.wren.weighanchor
```

## Configuration

Both are optional; the defaults are what the plist and the registry assume.

| Variable | Default | What it does |
| --- | --- | --- |
| `WEIGH_ANCHOR_DIR` | `~/Projects/WeighAnchor` | Where the checkout is. Wren serves `<dir>/dist`. |
| `WEIGH_ANCHOR_PORT` | `3002` | The game service's port. Must match `PORT` in the plist. |

`WREN_PUBLIC_URL` is reused, not games-specific: it makes the link `list_games`
returns absolute, so it's tappable when the answer is read on a phone.

## When a game shows as unavailable

The `/games` page probes each game before offering it, so a broken game is greyed
with a reason rather than served as a dead board. Two reasons:

- **"not built yet"** — no `dist` directory. Run the build above.
- **"its model service isn't running"** — nothing is listening on the port.
  Check `launchctl print gui/$(id -u)/local.wren.weighanchor` and the service log
  at `~/Projects/WeighAnchor/logs/service.log`.

The build is reported first when both are wrong, because it's the actionable one.

## Adding a game

1. **Add an entry to `games()` in `agent/tools/games.py`** — `id`, `name`,
   `blurb`, `players`, `path` (`/games/<id>/`), `dist`, `api_port`, `note`. The
   `id` in `path` must match the entry's `id`; that's what the routes resolve on,
   and what the bundle's `VITE_BASE` has to be built with.
2. **Give it a launchd plist in `launchd/infra/`** if it needs a service. Bind it
   to loopback.
3. **Map it in `chat/insights.py:TOOL_SERVICES`** if you added a tool — a
   drift-guard test fails on an unmapped tool.
4. **Document its variables in `config/.env.example`.**

Nothing else. The page, the nav entry, the proxy and the chat tool all read the
registry, so they pick a new game up on their own.

## Testing

`tests/test_games.py` covers the registry and availability; `tests/test_routes_games.py`
covers auth gating, bundle serving and the proxy. No test may reach a real game
service: `tests/conftest.py:_isolate_games` stubs the liveness probe off and points
the checkout at a tmp dir suite-wide, so `available` doesn't depend on whether the
developer happens to have the dev server running.
