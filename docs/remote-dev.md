# Developing Wren from another machine

Wren runs on the Mac mini and only there. This describes working on it from a
second machine — the case that prompted it was a Windows laptop, but the setup
is the same from any client with an SSH client.

## Why there is no second checkout

The obvious approach — clone the repo on the laptop, edit, push, pull on the
Mac — does not work, and the reason is worth stating so it isn't re-proposed:

- `agent/store.py` imports `fcntl`, which is POSIX-only. Nearly every module
  imports `store`, so `pytest` fails at collection on native Windows. A shim
  would be needed just to run the tests.
- `launchd` runs the chat server and every scheduled task. There is no
  equivalent on the client.
- The model (Ollama), the Google credentials, the Chrome history database, and
  the Obsidian vault are all Mac-side. Even with the two problems above solved,
  a laptop checkout could not run a scheduled task or a chat turn end to end.

So the client gets **no copy of the repo**. It gets a terminal on the Mac. The
files, the venv, the tests, and the model stay put. Nothing is copied, so
nothing can drift, and there is no deploy step — an edit is live the moment it
is saved.

## Setup

Once, in this order.

**1. Mac: turn on Remote Login.** System Settings → General → Sharing → Remote
Login. Set *Allow access for* to your user only rather than All users. Confirm
with `nc -z -w 2 localhost 22`.

**2. Client: generate a key.**

```
ssh-keygen -t ed25519 -C "<client hostname>"
```

**3. Mac: authorize it.** Append the full contents of the client's
`id_ed25519.pub` to `~/.ssh/authorized_keys` (mode 600, in a `~/.ssh` of mode
700). Append the *whole line* — a paste that loses the leading `ssh-ed25519 `
type field leaves sshd unable to classify the key, and it silently falls back
to a password prompt rather than reporting a malformed entry.

**4. Client: add an SSH alias.** In `~/.ssh/config` (`%USERPROFILE%\.ssh\config`
on Windows):

```
Host wren
    HostName <mac tailscale IP>
    User <mac username>
    ServerAliveInterval 30
    ServerAliveCountMax 6
```

The `ServerAlive` pair keeps a session from dying unannounced when the client
sleeps. Verify with `ssh wren` — it should land on a shell with no password
prompt.

The Tailscale address is what makes this reachable from anywhere without
exposing port 22 to the internet; it is the same network surface the chat
server already relies on (see the security-model section of the README).

## Working

```bash
ssh wren
tmux new -A -s wren
```

`tmux` is not optional in practice. A plain SSH session dies with the network,
and it takes every process started inside it — a long test run, a Claude Code
session — down with it. `-A` means *attach if the session exists, create it if
not*, so the same command both starts the day and recovers from a dropped
connection.

From there it is an ordinary local checkout: `.venv/bin/pytest`, `npm test`,
`git`, and `./launchd/restart-server.sh` after editing anything the chat server
imports.

**Claude Code needs nothing special.** It has no SSH mode and does not want one
— it operates on the filesystem of the machine it runs on, so running it in the
SSH session is exactly right. (The `--remote-control` flag is unrelated: it
drives a session from a second device, and is not a remote-filesystem feature.)

An IDE with a remote-SSH mode (VS Code's Remote - SSH, or the JetBrains
equivalent) is an alternative front end to the same session. It adds a file tree
and inline diffs; it adds no capability. Either front end is fine.

## What this does not solve

The launchd agents live in the `gui/<uid>` domain, which is only populated after
a login at the physical console — and FileVault means that login cannot be
automated. **After a reboot, Wren stays down until someone logs in at the
machine.** SSH will not do it: an SSH session gets a background security
session, so `launchctl bootstrap gui/<uid>` fails from there. Plan reboots for
when you have physical access.

Everything else — including a chat-server restart, which acts on an
already-loaded agent — works fine over SSH.
