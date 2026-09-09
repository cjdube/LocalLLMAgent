# Settings — `/settings` and `config/settings.json`

Every configuration key Wren has is readable and editable from the phone, at
`/settings`, behind the same session gate as the rest of the UI. Before this,
changing the model or the morning brief's window meant an SSH session, a text
editor and a `launchctl` line.

Three files, three jobs:

| File | Holds | Edited by |
|---|---|---|
| `config/.env` | Secrets, and keys this repo has no row for | You, by hand |
| `config/settings.json` | Everything else, plus the preference sections | The `/settings` page |
| `agent/schema.py` | The table of what exists | A commit |

`config/settings.json` is gitignored. `agent/schema.py` is committed: it is the
*shape*, not the values.

## The table is the source of truth

`agent/schema.py` holds one frozen `Setting` row per key. The row — not the
call site — is what the page renders, what `agent/config.py` resolves a default
from, and what `agent/migrate_settings.py` uses to decide whether a key moves.

A key with **no row is invisible** to all three, and that is deliberate.
`STRAVA_*`, `ANTHROPIC_API_KEY` and the `SCRIBEJAY_*` keys sit in some
developers' `config/.env` with zero readers here. No row means "not ours, leave
it alone".

Adding a setting is one row plus one `config.getenv` call. Two AST drift guards
in `tests/test_schema.py` keep the two in step, in both directions:

- every literal key passed to `config.getenv` must have a row;
- no key that has a row may still be read with a raw `os.getenv`.

The second one is the load-bearing half. A page that shows a field, accepts an
edit and saves it while the code reads somewhere else is worse than no page at
all.

## How one key resolves

`config.getenv(key)` replaces `os.getenv(key)`. Four layers, **first non-empty
wins**:

1. environment variable — a real one, or one loaded from `config/.env`
2. `config/settings.json` — what the page writes
3. the schema default
4. the caller's own `default=` argument

An empty string does not count as set. Exporting `KEY=` is how a shell unsets
nothing in particular; treating it as a value would pin the empty string past
every layer below.

### Why the environment still outranks the page

This looks backwards for a settings page until you count what depends on it:
251 `monkeypatch.setenv` calls across `tests/`, and every `WREN_X=...
.venv/bin/python -m ...` one-off on the command line. A file that outranked the
environment would break both, silently.

The cost is that a key still assigned in `config/.env` wins **forever**: the
field renders, accepts an edit, saves, and changes nothing. Two things pay that
cost off:

- `agent/migrate_settings.py` moves those keys out, once.
- `config.STARTUP_WARNINGS` names any that still overlap. The chat server logs
  them at startup and the page shows them as a banner at the top.

A row the environment answers comes back from the API as `source: "env"`, and
the page **locks the input** and names the command to run. Rendering an
editable box over a value that cannot move is the dishonest option.

## `applies` — when a saved value actually lands

ScribeJay, whose design this is copied from, is all short-lived launchd
processes, so every change there is live. Wren has a chat server that runs for
weeks. So each row says when its value lands:

| `applies` | Meaning | What you must do |
|---|---|---|
| `live` | Read per call in every process | Nothing. It is already in effect |
| `next_run` | Read only by launchd tasks, which re-import each run | Nothing. It lands at the next run |
| `restart` | Bound at import inside the chat server | Restart the chat server |

Strongest reader wins. An import-time read inside the chat server means you
must act, so it beats both others; a per-call reader needs nothing, so it beats
a task's next run.

`tests/test_schema.py` verifies the `restart` set both ways against the
server's **real import closure** — the modules actually reachable at import
from `chat/server.py`, read off `sys.modules`. It deliberately does not try to
tell `live` from `next_run`: that is not statically decidable, and guessing
`live` when the truth is `next_run` is a harmless under-promise.

The page turns this into three different banners after a save, computed from
the keys that **actually changed** rather than from what the form submitted:

- green, *"Saved and already in effect"* — the `live` keys;
- green, *"lands at their next run. Nothing to do."* — the `next_run` keys;
- yellow, with the exact command and a copy button — the `restart` keys.

Re-saving an untouched form raises nothing. A banner nobody needs is a banner
that trains you to ignore the ones you do.

There is **no restart button**. The server runs under launchd `KeepAlive`, so a
route that killed its own process would be a self-DoS if the save that preceded
it was the wrong one. The page shows the command instead:

```bash
launchctl kickstart -k gui/$UID/local.wren.wren
```

The label is `local.wren.wren`, not `local.wren.chat` — the plist is named for
the agent, not the module it runs. A wrong label does not error usefully; it
prints "Could not find service … in domain for user gui", which reads like a
permissions problem. `tests/test_schema.py` pins the string against the real
plist in `launchd/`.

## Secrets are never sent to the browser

Rows marked `secret: true` — ten today. For those, the API sends **no `value` key
at all** — absent, not redacted, not an empty string — and only `is_set`. There
is no shape of the response that carries a token, so no later edit to the page
can render one by accident.

The page gives a secret no input either. There is nothing to put in one, and a
blank box beside a live token invites you to retype it into a form that would
refuse the save. `config.set_value` refuses a secret whatever the caller is.

Secrets stay in `config/.env`, which is the file already protected as a
credential store. Moving one would buy nothing and put a credential in a second
place. `tests/test_routes_settings.py` sets every secret to a marker string and
asserts none of them appears anywhere in the whole response body — the row, the
preferences, or a warning.

## Locked rows

Some rows are `editable: false` and carry a `reason` the page displays — nine
today, the ports and hosts and the paths another file already pins.
`WREN_CHAT_PORT` is the clearest: the launchd plist binds that port, so
changing it here would move the server away from its own supervisor. A `reason`
is required whenever `editable` is false; a locked field with no explanation
reads as a bug.

Locked is locked against **editing**, not against living in the document.
`agent/migrate_settings.py` moves them like any other key, through
`config.set_value(..., allow_locked=True)` — the one caller that passes it. All
type, range and choice checks still run.

## Preferences are sections of the same document

The personal sections — `persona`, `calendar`, `sports` and the rest — used to
live in `config/preferences.json`. They are now sections inside
`config/settings.json`, edited on the same page as a JSON block each, and
validated by the same functions `tests/test_prefs.py` asserts through. Their
keys are documented in [preferences.md](preferences.md).

A section is saved **whole, never merged**. A bad shape is refused with the
validator's own sentence before anything is written, and the page shows that
sentence against the section it came from. Bad JSON is caught in the browser,
so a stray comma costs no round trip.

`config/preferences.example.json` is still the committed template, and still
supplies `schema.STRUCTURED_DEFAULTS` — the shape a fresh clone boots with.

## One group on screen at a time

The page has thirteen groups and about sixty fields. Stacked, finding one meant
scrolling past all the others, so the groups are tabs: a rail down the left on a
laptop, and under 720px a single row of chips that scrolls sideways. Thirteen
chips wrapped onto four lines would eat the screen the tabs exist to give back.

The rail is built in `chat/static/settings-form.js` from the group order
`GET /api/settings` already sends. Nothing on the server knows about it.

Each tab has an accent, the way ScribeJay's does: a dot in the rail, a wash and
a left border on the open tab, and a rule under the panel heading, so the rail
reads as thirteen places rather than thirteen words. Text never sits on an
accent, only those four things, so no accent has a contrast bar to clear.
Colours are handed out **by position, not by group name** — a name table in
that file would be a second copy of something `agent/schema.py` already owns, in
a file served with no auth check, and by position a group added to the schema
gets a colour with no edit to the script at all.

Two things follow from the Save button writing every tab at once:

- **Every card stays in the document**, hidden rather than absent. `collect()`
  reads the inputs on the tabs you are not looking at, and it must.
- **A refused field flags its tab and opens it.** Otherwise the page says "Not
  saved" and shows nothing that explains why, because the bad field is three
  tabs away. Both halves are asserted in `tests/settings-form.test.js` — a flag
  with no jump is as useless as a jump with no flag.

The open tab survives the re-read that follows a save. Landing back on "Model"
after every save would undo the point.

ScribeJay's settings screen does the same thing with CSS-only radio tabs
(`scribejay/cli/settings_form.py`), because that page ships no JavaScript. This
one already builds its whole DOM in a script, so the rail is built there too.

## Saving is all-or-nothing

`config.apply` raises on the first bad key, which is right for a script and
wrong for a form: one error per round trip makes you save six times to find six
mistakes. So `chat/routes_settings.py` validates everything first, against a
throwaway staged copy that nothing can write, and returns **every** bad field
at once.

A rejected save leaves `config/settings.json` byte-identical — including the
good values that were in the same batch. A half-applied config is how a 4:30 AM
task dies unattended.

Writes go through `agent/store.py`'s `locked()` + `atomic_write_json()`: the
mandated primitive, already 0600 through `mkstemp`, plus the cross-process
`flock` that the threaded chat server and the launchd workers both need.

Reads deliberately do **not** go through `store.load_json()`. Its corrupt-file
quarantine renames the file to `<name>.corrupt-<ts>`, which is right for a
machine-written store and wrong for one a person may hand-edit. A file that
will not parse is logged and read as empty; nothing is moved.

The route logs key names only, never values. A configuration change is worth an
audit line; the values are what the rest of the module exists to protect.

## Moving an existing install

Run the migration once. It is a dry run by default, because it touches the only
credential file on the machine:

```bash
.venv/bin/python -m agent.migrate_settings
```

Read the plan it prints, then:

```bash
.venv/bin/python -m agent.migrate_settings --apply
```

What it does, in order: backs `config/.env` up to
`config/.env.pre-settings-<timestamp>`, writes the settings document, rewrites
`.env` without the moved keys, and renames `config/preferences.json` to
`preferences.json.migrated`.

What moves and what stays:

- a non-secret key with a schema row **moves**, locked ones included;
- a secret row **stays** — ten of them;
- a key with no row **stays**, reported as "not recognised".

Nothing is clobbered: a key already in the document keeps the value it has and
is reported as skipped, so a second run is safe and does no work. A value the
schema refuses stays in `.env` and is reported; it never aborts the rest.

`preferences.json` is renamed only when every section moved. Renaming one that
still holds a refused section would hide that section behind a name nothing
loads, and your persona would go missing with no message.

Restart the chat server afterwards. The document is read at import and on save,
so a hand-edit or a migration is not seen by the running process until it
restarts — `/settings` will render schema defaults where you have values, and
the running server will resolve them that way too.

Your values are safe if you forget. A save merges onto the document as it is on
disk, under the lock, so it can no longer write this process's stale copy back
over a migration; `agent/config.py:flush` logs a WARNING naming the keys that
arrived from elsewhere, and reloads. Before that merge existed, one unrelated
save undid the whole migration, and the values were already out of `config/.env`
by then.

## Where the code lives

| File | Job |
|---|---|
| `agent/schema.py` | The table: one `Setting` row per key, the groups, `PREFERENCE_SECTIONS`, `RESTART_COMMAND` |
| `agent/config.py` | The four-layer resolve, `source_of`, `is_set`, the staged write and `apply` |
| `agent/migrate_settings.py` | The one-time move out of `.env` and `preferences.json` |
| `chat/routes_settings.py` | The `/api/settings` blueprint — the gate, the validation, the log line |
| `chat/views/settings.html` | The page shell. In `views/`, so Flask cannot serve it unauthenticated |
| `chat/static/settings-form.js` | The form. Public file: no keys, no values, no copy of the schema |

The script holds no schema on purpose. `chat/static/` is served by Flask at
`/static/<file>` **with no auth check**, so a table shipped to the browser as a
literal would be readable by anyone who can reach the server. It renders only
what the gated endpoint hands it, and it writes with `textContent`, never
`innerHTML`.

Store paths resolve per call from `WREN_SETTINGS_FILE`, `WREN_ENV_FILE` and
`WREN_PREFERENCES_FILE`, not from module constants. `monkeypatch` stops at the
process boundary and the suite spawns a real child interpreter, which has to
inherit the redirect. `tests/conftest.py` sets `WREN_SETTINGS_FILE` **above**
its own agent imports — `agent/config.py` loads the file layer at import, so a
redirect below them is always too late. `tests/test_conftest.py` asserts that
ordering directly, because an empty document and a redirected one look
identical until the real one exists.

## Related

- [preferences.md](preferences.md) — what each personal section means
- [security-model.md](security-model.md) — the trust boundaries this page sits inside
- [limits.md](limits.md) — where every bound is defined
- [module-map.md](module-map.md) — where each part lives
