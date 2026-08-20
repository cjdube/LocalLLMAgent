# External task roots — reporting on another repo's launchd jobs

Wren's dashboard reads `launchd/*.plist` for schedules and `logs/*.log` for run
history. `WREN_EXTERNAL_TASK_ROOTS` points it at the same two directories inside
a **sibling repo**, so that repo's scheduled jobs appear as ordinary rows —
schedule, next run, run history, duration charts, the `/map` routines band, and
`log_inspector`'s "was due and didn't run" detection.

```
WREN_EXTERNAL_TASK_ROOTS=wiki=~/Projects/ObsidianWikiAgent
```

Comma-separated `name=path` entries. The name prefixes every task key from that
root (`learnings-ingest` → `wiki-learnings-ingest`), which keeps two repos from
colliding and keeps the key usable as a URL segment in `/api/runs/<task_key>`.
Unset means Wren's own tasks only. A root that doesn't exist is skipped, not
raised — a moved or unmounted checkout means "no tasks from there", not a 500 on
every dashboard poll.

## Why federate instead of merging the repos

The only external root today is
[ObsidianWikiAgent](https://github.com/cjdube/ObsidianWikiAgent), which runs
three jobs against the `llm-wiki-learnings` vault:

| Job | Schedule | What it does |
| --- | --- | --- |
| `wiki-learnings-ingest` | Daily 9:00 AM | Files new `raw/` sources into `wiki/` concept pages |
| `wiki-learnings-lint` | Sundays 10:00 AM | Audits the vault (structural checks + a Gemini judgment pass) |
| `wiki-learnings-snapshot` | Daily 11:00 PM | Commits and pushes the vault to its git remote |

That vault is load-bearing for Wren — it's what `agent/tools/wiki.py` reads,
where `tasks/daily_chrome_learnings.py` and friends write, and the source of the
lens pages `evaluate_against` judges against. So the jobs' health matters here.
The *code* doesn't belong here:

- ObsidianWikiAgent is vault-agnostic and serves more than one vault. Merging
  would move a second vault's plumbing into Wren.
- It's MIT-licensed and publishable, with a stated no-dependency-on-Wren design.
- The `raw/` → `wiki/` handoff is already a deliberate seam. The docstring in
  `agent/tools/wiki.py` records that two `raw/`-reading tools were built and
  then removed for reaching across it.

What was missing was observability, not code co-location.

## The contract an external repo has to honor

Wren parses run history out of log files, so an external repo's jobs show up
only if their logs look like Wren's. Three requirements:

1. **The formatter.** `"%(asctime)s [%(levelname)s] %(message)s"` — the shape
   `chat/insights.py:_LINE_RE` matches. Anything else parses as zero runs.
2. **Run boundaries.** A line starting with `Starting …` and containing `run`
   opens a run; a line containing `complete` and `run` closes it successfully
   (see `_is_run_start` / `_is_run_success`). `wiki_ingest.py` already logged
   `"Starting wiki ingest run for vault: …"` / `"Wiki ingest run complete"`
   before any of this existed — the convention is genuinely shared, not
   retrofitted.
3. **One record per line.** `_parse_runs_uncached` treats any line that doesn't
   match `_LINE_RE` as a traceback continuation and appends it to the run's
   `error` field. A multi-line report logged as a single record therefore
   renders a clean run as a failure blob. `wiki_lint.py` logs one line per
   finding for exactly this reason.

### `WREN_RUN_LOG`

By default Wren looks for the run log at `<root>/logs/<key>.log`, where `<key>`
comes from the plist's `StandardOutPath` basename. An external repo needn't name
its logs that way — ObsidianWikiAgent's `learnings-ingest` job writes
`wiki_ingest.llm-wiki-learnings.log` — so a plist can name the file explicitly:

```xml
<key>EnvironmentVariables</key>
<dict>
    <key>WREN_RUN_LOG</key>
    <string>/Users/you/Projects/ObsidianWikiAgent/logs/wiki_ingest.llm-wiki-learnings.log</string>
</dict>
```

One explicit key beats hard-coding a sibling repo's naming rule into Wren, and
it follows a pattern that repo already uses (`WIKI_LAUNCHD_LOG` repeats a path in
`EnvironmentVariables` so the script can find the file launchd opened).

## Report-only: no "Run now"

External tasks get "See runs" but not the "Run now" button, and
`RunManager.start()` refuses their keys. Two reasons, either sufficient:

- The button spawns `.venv/bin/python -m <module>` from Wren's root, which is a
  Wren convention. Another repo's plist need not be a `-m` invocation at all.
- Ollama runs with `OLLAMA_NUM_PARALLEL=1`. The wiki ingest routinely takes one
  to three hours, and on 2026-08-03 a wedged MLX runner had it retrying until
  11:54 — nearly three hours during which no other consumer could get a model
  call through. A button that starts that from a phone is a footgun.

Use `launchctl start local.wikiagent.<vault>-ingest` when you want one on
demand.

## What the health rollup does and doesn't add

ObsidianWikiAgent already pushes its own ntfy alert when a run fails or blows its
budget, so this isn't about crash notification. What it adds is **absence**:
`log_inspector`'s Signal B goes through `discover_tasks()`, so a federated job
that launchd never fired — or that started and never finished — now lands in the
7 AM rollup like any Wren task. Signal A (the line scan) reads each external
root's `logs/` too, skipping `*.launchd.log` there for the same
double-count reason it skips Wren's.

## The vault health card

Everything above answers **"did the job run?"** — it reads launchd schedules and
log markers. The dashboard's *Learnings wiki* card answers a different question,
**"is the wiki actually in good shape?"**, by reading the vault instead of the
logs (`vault_health()` in `chat/insights.py`, served at `/api/vault_health`).

The two don't overlap. An ingest that skips a source still starts, still logs
`Wiki ingest run complete`, and still shows a green row — the run *was* a
success; the outcome was incomplete. `Daily-YouTube-2026-08-02.md` sat unfiled
for two days that way, behind a healthy-looking row.

Four signals:

- **Pages** — concept pages in `wiki/`, excluding `index.md` and `log.md`.
- **Files waiting** — raw sources whose basename isn't in `wiki/.ingested.json`,
  oldest first, and the card warns at two days. One file waiting at 8am is the
  normal state before the 9am ingest; age is what separates that from stuck.
- **Last backup** — the vault's last commit date and how many commits the remote
  doesn't have. Read against the local remote-tracking ref, never a fetch, which
  is also what makes it the right signal: the snapshot job's push is what
  advances `origin/main`, so a push that keeps failing leaves this climbing.
- **Binaries** — reported apart from the queue, never as pending. The ingest
  excludes them by design (read as text, a PNG produced a page of fabricated
  claims that each carried a citation, 2026-08-02), so one counted as "waiting"
  would cry wolf forever.

Two details worth knowing. Pending is matched on **bare basenames**, mirroring
`list_raw_files` in the sibling repo: `raw/` gets sorted into subdirectories
after the fact, and `.ingested.json` records bare names so that sorting doesn't
trigger a re-ingest. And the text/binary test is git's — a NUL byte in the first
8 KB — reimplemented here rather than imported, since the whole point is that
neither repo depends on the other.

This card reads `raw/`, which the module docstring in `agent/tools/wiki.py`
tells you not to do. That rule is about *model-facing tools* reading raw file
contents, and it stands. This counts filenames, isn't a tool, and can't
silently return nothing the way those tools did — zero pending is itself a
number on the page.

## Adding another root

1. Make the repo's logs satisfy the three requirements above.
2. Add `name=path` to `WREN_EXTERNAL_TASK_ROOTS` in `config/.env`.
3. Set `WREN_RUN_LOG` in each plist if the log names don't match the keys.
4. Add the new task keys to `ROUTINE_USES` in `chat/insights.py` so `/map` shows
   what they touch.
5. Restart the chat server — `.env` is read at startup.

Check it without the server first:

```bash
.venv/bin/python -m chat.insights
```
