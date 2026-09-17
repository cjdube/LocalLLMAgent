# Games

Wren hosts games you play *with* her — a `/games` page listing what's playable,
and a `list_games` chat tool so asking "what can we play?" answers with a link.

Wren doesn't implement any game. Each one lives in its own repo with its own
rules engine and UI; Wren provides the front door, and — for a game whose
opponents need a model — the local model those opponents think with.

Two games are registered:

- **Weigh Anchor** (`~/Projects/WeighAnchor`) — a word-deduction card game whose
  AI seats think with Wren's local model.
- **Train Game** (`~/Projects/TrainGame`) — a railway route-building game. Its
  opponent runs as a separate agent process against the game's own server, so no
  model call crosses this proxy.

## How a hosted game fits together

```
phone ──HTTPS──> tailscale serve ──> Wren :8420 ─┬─ /games            the list
                                                 ├─ /games/<id>/…     the built bundle, off disk
                                                 └─ /games/<id>/api/…
                                                            │ proxy, loopback only
                                                            ▼
                                                   game service :3002 / :4173
                                                            │
                                                            ▼
                                                      Ollama :11434   (Weigh Anchor only)
```

Three properties of that shape are deliberate:

- **The game is mounted under Wren's origin, not given a port of its own.** The
  game has no authentication — it expects to sit behind something that does. Wren's
  token login and the single `tailscale serve` front door are that something.
  Publishing the game service on its own tailnet port would be a way in that skips
  the token, which is why `server/index.ts` binds `127.0.0.1` by default.
- **The proxy is dumb.** It forwards the body and hands back the response. Prompt
  construction, schema validation, the game's own auth and its fallbacks all belong
  to the game's own service; a second copy here would be a second thing to drift.
- **How much of a service the proxy opens is decided per game.** See below.

### The two gates on the proxy

`chat/routes_games.py:game_api` is one route with two different bounds, and
confusing them is the way to open a service by accident.

| | who gets it | shape | timeout |
| --- | --- | --- | --- |
| `/api/ai/<endpoint>` | every registered game | POST only, one flat segment | 160s, or 620s for `warmup` |
| the rest of `/api/…` | only a game whose registry entry sets `proxy_api: True` | GET and POST, nested paths, `Authorization` forwarded | 15s |

Weigh Anchor's browser calls nothing but AI endpoints, so it stays on the first
row and its service's other routes stay unreachable from a browser. Train Game
runs its whole match over its own HTTP API — board, session, view, actions,
handover — so it needs the second, and sets `proxy_api: True` to ask for it.

Both rows reject `.` and `..` segments. `requests()` normalizes dot segments when
it builds the URL, so without that check a path of `../internal/x` would reach the
service as `/internal/x` — any route on it. A browser normalizes before sending;
curl or a script does not.

Only the `Authorization` header crosses the proxy. Wren's own session cookie must
not reach a game service, and forwarding `Host` or `Origin` would trip Train
Game's loopback-name check, since `requests()` sets `Host` from the loopback URL.

## The two constraints worth knowing before you play

**Weigh Anchor's turns and chat turns queue behind each other.** Its AI seats
think with the same local model chat uses, and Ollama serves one generation at a
time. A message sent to Wren mid-game waits for the current seat to finish, and
vice versa. This is a latency surprise, not a bug — both sides hold the model with
`keep_alive` precisely so neither pays a reload. Train Game does not have this
problem: its opponent plays from its own process and its match API never calls a
model.

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

## Deploying Train Game

Same rule, same reason — the screen is a build artifact of the other repo:

```bash
cd ~/Projects/TrainGame
VITE_BASE=/games/train-game/ npm run -w @traingame/ui build
```

Note the workspace: the built screen lands in `packages/ui/dist`, not at the
checkout root, and that is what the registry entry points at.

`npm run game` in that repo rebuilds the UI **plain** before starting the server,
which overwrites a bundle built for Wren. Build for one mount point at a time:
plain to play standalone at `http://127.0.0.1:4173`, with `VITE_BASE` to play
through Wren.

Then start the server:

```bash
cd ~/Projects/TrainGame
npm run -w @traingame/server game
```

**There is no launchd plist for it yet**, so it does not come back after a reboot
and `/games` greys it out until you start it by hand. Add one in `launchd/infra/`
the way Weigh Anchor has, when it is worth it.

## Configuration

All four are optional; the defaults are what the plist and the registry assume.

| Variable | Default | What it does |
| --- | --- | --- |
| `WEIGH_ANCHOR_DIR` | `~/Projects/WeighAnchor` | Where the checkout is. Wren serves `<dir>/dist`. |
| `WEIGH_ANCHOR_PORT` | `3002` | The game service's port. Must match `PORT` in the plist. |
| `TRAIN_GAME_DIR` | `~/Projects/TrainGame` | Where the checkout is. Wren serves `<dir>/packages/ui/dist`. |
| `TRAIN_GAME_PORT` | `4173` | The game server's port. Must match the port it is started on. |

`WREN_PUBLIC_URL` is reused, not games-specific: it makes the link `list_games`
returns absolute, so it's tappable when the answer is read on a phone.

## When a game shows as unavailable

The `/games` page probes each game before offering it, so a broken game is greyed
with a reason rather than served as a dead board. Two reasons:

- **"not built yet"** — no `dist` directory. Run the build above.
- **"its model service isn't running"** — nothing is listening on the port. For
  Weigh Anchor, check `launchctl print gui/$(id -u)/local.wren.weighanchor` and
  the service log at `~/Projects/WeighAnchor/logs/service.log`. Train Game has no
  plist, so this just means you have not started it.

The build is reported first when both are wrong, because it's the actionable one.

## Adding a game

1. **Add an entry to `games()` in `agent/tools/games.py`** — `id`, `name`,
   `blurb`, `players`, `path` (`/games/<id>/`), `dist`, `api_port`, `note`, and
   `proxy_api`. The `id` in `path` must match the entry's `id`; that's what the
   routes resolve on, and what the bundle's `VITE_BASE` has to be built with.
   Set `proxy_api: False` unless the game's browser code has to reach its service
   beyond `/api/ai/` — see "The two gates on the proxy" above.
2. **Give it a launchd plist in `launchd/infra/`** if it needs a service. Bind it
   to loopback.
3. **Map it in `chat/insights.py:TOOL_SERVICES`** if you added a tool — a
   drift-guard test fails on an unmapped tool.
4. **Document its variables in `config/.env.example`, and give each one a
   `Setting` row in `agent/schema.py`.** A key with no row is invisible to
   `/settings`, and an AST guard in `tests/test_schema.py` fails without it.
5. **Point `tests/conftest.py:_isolate_games` at a tmp dir for the new checkout**,
   so the suite never reads the real one.

Nothing else. The page, the nav entry, the proxy and the chat tool all read the
registry, so they pick a new game up on their own.

## Testing

`tests/test_games.py` covers the registry and availability; `tests/test_routes_games.py`
covers auth gating, bundle serving and the proxy. No test may reach a real game
service: `tests/conftest.py:_isolate_games` stubs the liveness probe off and points
the checkout at a tmp dir suite-wide, so `available` doesn't depend on whether the
developer happens to have the dev server running.
