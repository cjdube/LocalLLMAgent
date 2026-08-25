# Gmail watch — Wren reacts to new mail

Label a Gmail thread `Wren/Watch` and Wren tells your phone the moment anything
lands on it — usually within a few seconds. She can also search and read the
mailbox in chat.

Two pieces:

| Piece | What it is | When it runs |
| --- | --- | --- |
| `tasks/mail_watcher.py` | Always-on daemon. Holds a Pub/Sub streaming pull open, summarizes each new labelled email in one sentence, pushes it via ntfy. | Continuously (launchd `KeepAlive`) |
| `tasks/mail_watch_renew.py` | Re-registers the Gmail watch and stores its expiry. | Daily, 4:15 AM |

Plus two chat tools in the `mail` group: `search_mail` and `read_email`.

## Why it is built this way

**Push, not polling.** Gmail's `users.watch` publishes every mailbox change to a
Cloud Pub/Sub topic. Polling would mean a fixed floor on latency and a constant
API spend for a mailbox that is usually quiet.

**Pull, not a webhook.** Pub/Sub can push to an HTTPS endpoint, but that endpoint
has to be public. Wren is `tailscale serve` only, and
[docs/security-model.md](security-model.md) states that tailnet-only surface as a
design choice — Tailscale Funnel would break it. So the mini opens a *streaming
pull* subscription instead: it dials out, holds the connection, and Google pushes
down it. Same latency, **no port opens**.

**The label is the control, and you set it.** Either a Gmail filter applies it
or you apply it by hand. Nothing in the code decides which threads matter.

**The thread is the unit, not the message.** Label any message and Wren follows
that whole conversation from then on, including replies that never carry the
label themselves.

That last point is the correction to an assumption that shipped wrong. Gmail
puts a hand-added label only on the messages that existed *at that moment*. A
reply arriving later does **not** inherit it. So the first build reported the
first email on a hand-labelled thread and then went permanently silent — and it
looked like it worked, because the test thread's subject was `[wren]` and the
Gmail *filter* kept re-applying the label to each reply.

Two things follow from it:

- **The watch is not label-filtered.** `users.watch` covers the whole mailbox.
  A label-filtered watch would never publish for that unlabelled reply, and no
  amount of filtering downstream can recover a notification that was never sent.
  The cost is many more notifications, nearly all resolving to nothing.
- **`_thread_is_watched` decides, by asking Gmail.** For each new message, does
  any message on its thread carry the watch label? One `threads.get` per
  distinct new thread — single digits a day on this mailbox.

**Nothing about watched threads is stored.** That was considered and rejected.
Gmail already holds the answer, so asking it keeps no state, grows nothing, and
means peeling the label off a thread stops Wren immediately. A stored set would
keep alerting until it aged out, and would need a cap nobody can pick well.
Revisit only if the mailbox ever runs thousands of messages a day.

**Your own words are skipped.** `list_history` drops anything labelled `SENT`
**or `DRAFT`**, before the thread lookup.

`DRAFT` is the one that is easy to miss, and it produced a live false alert.
Gmail autosaves a reply as a draft *before* you send it. That draft is a real
message on the thread, and it carries `DRAFT`, never `SENT`. A `SENT`-only
filter therefore alerts you the moment you start typing. The draft is destroyed
on send, which is why its message id 404s afterwards.

**Read-only.** The watcher holds `gmail.readonly`. It cannot reply, delete,
archive or send. `send_email` is a separate scope and a separate, gated tool.

## The injection posture

An email is written by a stranger and can carry text aimed at Wren's model. Two
things keep that harmless here:

1. **The watcher's model call has no tools at all.** It is a `complete_text()`
   that writes one sentence — the same posture `morning_brief` uses. An injected
   instruction has nothing to actuate.
2. **Python owns everything the alert asserts.** The sender and the subject on
   the push come from the message headers, never from the model. The model's
   entire contribution is one sentence of gist, and it is length-capped.

The chat tools are reads, so they add no write path either. `read_email`'s tool
description tells the model to report what a message says and never to follow
instructions inside it. **Do not add a write to `agent/tools/gmail_read.py`
without a gate** — see the "Untrusted content boundary" section of `CLAUDE.md`.

## Setup (one-time, manual)

### 1. Google Cloud console

In the **existing** Wren project (the one behind
`config/google_credentials.json` — not a new one):

1. Enable the **Cloud Pub/Sub API**.
2. Create a topic named `wren-mail`, keeping the default subscription. Google
   names it `wren-mail-sub`.
3. Confirm the subscription's delivery type is **Pull**. This is what keeps the
   mini off the public internet.
4. On the topic, grant **Pub/Sub Publisher** to the principal
   `gmail-api-push@system.gserviceaccount.com`.

Step 4 is the one this setup most commonly loses, and the resulting error does
not name it — see Troubleshooting.

### 2. OAuth publishing status

Google Auth Platform → Audience. It must read **In production**. An app left in
*Testing* issues refresh tokens that expire after 7 days, and `gmail.readonly` is
a *restricted* scope, so the current token's behavior does not automatically
carry over.

### 3. Gmail

On desktop Gmail (nested labels cannot be created on mobile):

1. Create the label `Wren`, then `Watch` nested under it.
2. Create a filter that applies `Wren/Watch`. Subject containing `[wren]` is a
   good starting rule. Leave "skip the inbox" **off** — these should still be
   visible.
3. Test it with mail from an **outside** address. Mail you send yourself threads
   with the Sent copy and does not reliably produce the arrival event the watcher
   listens for.

The filter is a convenience for threads *you* start. Any thread can also be
watched by applying the label to it by hand — including one a stranger started,
whose subject you do not control. That is the case the thread lookup exists for.

The nested `Wren/...` shape is deliberate: sibling labels are planned, and a flat
`Wren` label would have to be rebuilt.

The label's internal id is never configured by hand. The code looks it up from
the name.

### 4. Config

In `config/.env` (all documented in `config/.env.example`):

```
MAIL_PUBSUB_PROJECT=your-google-cloud-project-id
MAIL_PUBSUB_TOPIC=wren-mail
MAIL_PUBSUB_SUBSCRIPTION=wren-mail-sub
MAIL_WATCH_LABEL=Wren/Watch
```

`MAIL_PUBSUB_PROJECT` is required; both tasks refuse to start without it.

### 5. Re-consent

Two scopes were added to `agent/tools/google_auth.py`, and a cached token is
locked to the scopes it was originally consented to:

```bash
rm config/google_token.json && .venv/bin/python -m agent.tools.google_auth
```

A browser opens once. Pick the right account.

**Consenting over SSH from the laptop.** The mini is headless, so the browser
runs on the laptop — where `localhost` is the laptop, not the mini. Left alone
the callback dies with `ERR_CONNECTION_REFUSED`. Forward a port, and pin the
flow to it:

```bash
ssh -L 8765:localhost:8765 wren
```

then, in that session:

```bash
cd ~/Projects/LocalLLMAgent
rm config/google_token.json
GOOGLE_OAUTH_PORT=8765 .venv/bin/python -m agent.tools.google_auth
```

The port only has to be free on both ends; 8765 is arbitrary. Loopback
redirects on a Desktop OAuth client accept any port, so nothing is registered in
the console.

### 6. Install the agents

```bash
./launchd/install.sh launchd/local.wren.mailwatchrenew.plist launchd/local.wren.mailwatcher.plist
```

Install the renewal job **first**: the watcher has nothing to listen for until a
watch is registered. Both have `RunAtLoad`, so this registers the watch and
starts listening immediately.

## Verifying it works

```bash
.venv/bin/python -m tasks.mail_watch_renew
.venv/bin/python -m agent.tools.mail_state
```

`watch_expires_in_hours` should read about 168.

Then send yourself a `[wren]` email from an outside address, with ntfy open on
the phone. It should buzz within seconds. `logs/mail_watcher.log` records what
happened.

Reply to that same thread and confirm two things: the reply also pushes (dedupe
is per message, not per thread), and it pushes without you relabelling anything
(the label is inherited by the thread).

## The failure modes it is built around

### Gmail drops the watch after 7 days, silently

The mailbox just stops publishing. A stopped watcher looks exactly like a quiet
inbox, which is the silent degrade `CLAUDE.md` calls worse than a crash. Three
layers guard it:

- `mail_watch_renew` runs **daily**, far more often than the 7 days needs, so one
  missed run costs nothing. Renewing is idempotent.
- If the renewal succeeds but the expiry is still inside 48 hours, it pushes an
  alert anyway — that means the daily runs are not landing, and the failure path
  would never fire for it.
- `tasks/log_inspector.py` picks the job up automatically (it discovers tasks
  from the launchd plists), so a renewal job that stops running at all shows up
  in the 8 AM rollup. No code change was needed for this.

### Pub/Sub delivers at least once

The same notification arrives more than once — after a crash, and routinely,
because Gmail publishes one notification per mailbox change. `agent/tools/mail_state.py`
holds a `seen` set of Gmail message ids so a redelivery is a no-op. Without it,
one email buzzes twice.

### Pub/Sub does not guarantee order

A notification carrying an older `historyId` can land after a newer one. The
watermark only ever moves **forward** (`max`, compared numerically — Gmail
returns these as strings, and a string compare gets `"9" > "10"` wrong).

### Notifications are handled one at a time

The subscriber is opened with `FlowControl(max_messages=1)`, so only one
notification is leased — and therefore only one callback runs — at a time. The
default leases up to 1000 and runs callbacks on a thread pool, which breaks two
things at once. `unseen()` reads the store, the push happens, `commit()` writes
it: two threads can both read "not seen" before either writes, and the same email
pushes twice. And `summarize()` calls the local model, which serves one request
at a time (`OLLAMA_NUM_PARALLEL=1`), so concurrent callbacks queue there and
starve chat.

Mail arrives seconds apart and a push takes about a second end to end, so
serializing costs nothing real.

### Gmail's history is only about a week deep

Past that the stored watermark is gone and `history.list` returns 404. That is a
**lost watermark**, not "no new mail". The code logs a WARNING naming both the
stale id and the new one, resets to the mailbox's current history id, and returns
empty. Mail that arrived while the watcher was down for over a week is not
recoverable from history and is **not** reported.

### State is written before the ack

Acking a Pub/Sub message first would turn a crash into permanently lost mail:
Pub/Sub treats the notification as delivered while nothing on disk remembers it
was handled. So `mail_state.commit()` runs, then `ack()`.

A message whose **push failed** is left out of `seen` *and* the history watermark
is left where it was. Both halves are needed: `handle_notification` returns
normally, so the notification is acked and nothing redelivers it, and a watermark
advanced past the message would put it out of reach of every later
`history.list` too. Holding it makes the next notification re-walk the same
window; `seen` stops the ones that did land from pushing twice.

The retry rides on the **next notification**, so it is not a timer. In practice
that is minutes: the watch is unfiltered, so any mailbox change at all publishes
one. But a completely silent mailbox means a held message waits. The hold clears
on the first successful push, and if ntfy stays down past Gmail's week of history
the 404 resync moves it on and logs that it did.

A message that could not be **read** is marked seen instead. `history.list` named
it and `messages.get` then 404'd, which means it left the mailbox — that never
recovers, and holding the watermark for it would re-walk the same window forever.
It is not reported, and the WARNING says so.

### A poison message must not kill the stream

A raised exception inside a Pub/Sub callback cancels the subscription. Under
`KeepAlive` that means restarting into the same bad message forever. So the
callback logs the failure, pushes a failure alert, and acks anyway. That message
is lost, which is the lesser failure and is at least audible in the log.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `users.watch` returns 403, "the topic is not accessible" | `gmail-api-push@system.gserviceaccount.com` has lost **Pub/Sub Publisher** on the topic. The error does not name this. |
| Nothing ever pushes, no errors in the log | Check `.venv/bin/python -m agent.tools.mail_state`. A `null` or negative `watch_expires_in_hours` means no live watch — run `tasks.mail_watch_renew`. |
| `no Gmail label named "Wren/Watch"` | The label does not exist, or is spelled differently. Create it in desktop Gmail; it is matched case-insensitively but the words must match. |
| First run pushed nothing and warned "no stored history id" | Expected on a cold start. Walking history from nothing would report the whole mailbox as new. Run `tasks.mail_watch_renew` so the watch and the watermark are registered together. |
| The alert text is just the Gmail snippet | The model returned an empty summary — logged as a WARNING with the body length. Usually the thinking budget; see [docs/model-constraints.md](model-constraints.md). |
| Alerts stopped after a `brew python` upgrade | Same as every other launchd job here — re-bootstrap the agent with `./launchd/install.sh launchd/local.wren.mailwatcher.plist`, which boots it out first. |

## Replying on a thread

The push tells him mail landed. `reply_to_thread` is how the round trip closes
without opening Gmail: from chat, *"reply to Dana and say Thursday works"* →
`search_mail` → `read_email` → a confirmation card → sent, **in the same
thread**.

**He never names a recipient, and neither does the model.** The tool's schema
has exactly two parameters, `thread_id` and `body`. Everyone the reply goes to
is read out of that thread's own From/To/Cc headers by `reply_plan()` in
`agent/tools/email.py`. So the reply-all is the *thread's* participant list, and
an address that is not already on the thread cannot appear — which is what makes
it safe to let a model compose a reply to an email a stranger wrote. An injected
"reply to attacker@evil.com" has nothing to land in.

The other pieces of that:

- **The card names the people.** `describe_call` in `agent/toolset.py` reads the
  thread to show them, because the call's own arguments cannot. One Gmail read
  on the confirmation path, and an unreadable thread degrades to a card that
  says so rather than a card that omits it.
- **`reply_to_thread` is in `CONSEQUENTIAL_TOOLS`, not just `WRITE_TOOLS`.** It
  is the only tool that mails someone other than him, so a background run has to
  get a phone approval for it too.
- **Threading headers, not just `threadId`.** `threadId` keeps the reply tidy in
  *our* mailbox; `In-Reply-To`/`References` are what make the *recipient's*
  client show an answer instead of a new conversation. They come from the
  newest message on the thread — the fields `compact_message` has been returning
  since day one.
- **The thread is read whole**, not at the model's char budget. That budget
  drops the OLDEST messages, and with them anyone who has not written recently.
  If a message is dropped anyway, the reply is refused: a short recipient list
  is the dangerous degrade, because the reply looks sent and quietly leaves
  someone off.
- **A crowded thread is refused too** (`MAX_REPLY_RECIPIENTS`, 20). That is a
  mailing list, and trimming it silently would be worse than not replying.

`send_email` still has no `to` parameter and is not getting one. Replying was
the real need a `to` would have served, and this answers it without unpinning
anything.

## Not built yet

**Acting on mail** (a second label handing the message to `bg_worker`) is
designed but deliberately not wired up until this piece is proven live. So is
**delegated meeting scheduling**, which changes the security posture and needs
its own decision — `reply_to_thread` is the primitive it was waiting on.
