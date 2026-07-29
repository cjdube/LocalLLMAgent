# ntfy push server — setup

Wren's one outbound push channel: a **self-hosted** [ntfy](https://ntfy.sh)
server on the Mac mini, behind Tailscale. Scheduled tasks push a one-line alert
here when a run fails, `reminder_sweep` fires reminders through it, and
`bg_worker` sends tap-to-approve buttons through it. Optional — leave `NTFY_URL`
unset and push is simply off.

Self-hosting keeps alerts off the public internet, and `auth-default-access:
deny-all` plus a publish token means nobody else can inject fake notifications.
TLS is unnecessary: the link rides Tailscale's encrypted tunnel.

Code: `agent/tools/notify.py` (`notify`, `ntfy_health`). Behaviour — the
email-fallback asymmetry, what the health pill does and doesn't prove — is in
the README's "Failure alerts (push)" section.

## Why a container

**ntfy has no native macOS server.** `brew install ntfy` gives a *client-only*
binary (no `serve`), and even the official `darwin` release is client-only. So
on macOS the server runs as the Linux container in a lightweight VM — colima, no
Docker Desktop needed:

```bash
brew install colima docker
```

## Keeping colima up — not with `brew services`

**Don't use `brew services start colima`.** Homebrew's plist sets
`KeepAlive.SuccessfulExit=true`, which means *relaunch only after a clean exit* —
so a colima start that **fails** is never retried.

That is exactly what happened on 2026-07-11: the Mac rebooted, colima's VM died
mid-shutdown leaving stale state, the boot-time start failed with `vz driver is
running but host agent is not` (exit 1), launchd gave up, and the push channel
was down for four days. Nobody noticed, because nothing happened to need pushing.

Use the replacement service instead — `KeepAlive=true` (retry on failure too)
plus a wrapper that clears the stale state a crash leaves behind, which a bare
retry cannot do:

```bash
brew services stop colima           # hand off from Homebrew, if it's running
./launchd/install.sh launchd/infra/local.wren.colima.plist
```

colima lives in `launchd/infra/`, so a bare `./launchd/install.sh` skips it —
it's optional infrastructure, and keeping it out of `launchd/` also keeps it off
the dashboard's task list.

To stop colima deliberately, boot it out; `colima stop` alone won't stick, since
launchd immediately brings it back:

```bash
launchctl bootout gui/$(id -u)/local.wren.colima
```

That outage is also why `log_inspector` actively *probes* this channel every
morning rather than scanning for evidence of it: a dead push channel is invisible
to any log scan until something tries to use it, and by then the alert is the
thing being lost.

## The server

Create config + data dirs under your home (colima mounts `$HOME` into the VM, so
the container can read them):

```bash
mkdir -p ~/ntfy-server/{etc,lib,cache}
```

`~/ntfy-server/etc/server.yml`:

```yaml
base-url: "http://<mac-mini-tailscale-name>:2586"
listen-http: ":2586"
auth-file: "/var/lib/ntfy/user.db"
auth-default-access: "deny-all"      # no anonymous read OR publish
cache-file: "/var/cache/ntfy/cache.db"
upstream-base-url: "https://ntfy.sh" # iOS only: relays a *contentless*
                                     # wakeup ping so push is instant; the
                                     # message body is still fetched from
                                     # this server, never ntfy.sh.
```

Run the container (`--restart always` brings it back when colima restarts at
login):

```bash
docker run -d --name ntfy --restart always -p 2586:2586 \
  -v ~/ntfy-server/etc:/etc/ntfy \
  -v ~/ntfy-server/lib:/var/lib/ntfy \
  -v ~/ntfy-server/cache:/var/cache/ntfy \
  binwiederhier/ntfy:v2.26.0 serve
```

## Users, topic ACL, and the publish token

Create the publisher + subscriber, lock the topic down, and mint a publish token
(`NTFY_PASSWORD=...` sets passwords non-interactively):

```bash
docker exec -e NTFY_PASSWORD='<wren-pw>'  ntfy ntfy user add wren
docker exec ntfy ntfy access wren wren-alerts write
docker exec -e NTFY_PASSWORD='<owner-pw>' ntfy ntfy user add owner
docker exec ntfy ntfy access owner wren-alerts read
docker exec ntfy ntfy token add wren    # -> tk_...  set as NTFY_TOKEN
```

Wren publishes, the phone subscribes — separate accounts, so a leaked publish
token can't read the alert history.

## Wiring it up

Set in `config/.env`:

```
NTFY_URL=http://<mac-mini-tailscale-name>:2586/wren-alerts
NTFY_TOKEN=tk_...
```

On the iPhone, install the ntfy app → add a custom server pointing at the same
Tailscale URL → log in as `owner` (its password) → subscribe to `wren-alerts`.

Smoke-test end to end:

```bash
.venv/bin/python -m agent.tools.notify --message "hello from Wren" --title "Wren"
```

`WREN_PUBLIC_URL` is a separate knob: it's the chat server's public HTTPS base,
used to build the tap-to-approve buttons on background-job approval pushes. Unset
means those pushes still arrive, just without buttons (see
[background.md](background.md)).
