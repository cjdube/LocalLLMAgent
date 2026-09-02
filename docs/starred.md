# The starred view — how it works

The `/starred` view lists the user's starred GitHub repos as a table — **Repo ·
Language · What it does · Latest release · Installed** — sorted by
most-recently-pushed. The "what it does" blurb is written by the local model from
each repo's README and cached; the "latest release" and "installed" columns are
filled from separate caches. All are precomputed by scheduled tasks, so the page
loads instantly and the model never sits on the request path.

Code: `chat/views/starred.html` + the `/starred` page route in `chat/server.py`
and the `/api/starred` blueprint in `chat/routes_starred.py` (the view),
`tasks/starred_blurbs.py` (the cached-blurb job),
`tasks/starred_releases.py` (the cached-release job), `tasks/starred_installed.py`
(the installed-version job), and `fetch_starred_repos` / `fetch_readme` /
`fetch_latest_release` / `compare_versions` in `agent/tools/github_starred.py`
(the GitHub data and version comparison). Needs `GITHUB_TOKEN` (already used by
the morning brief).

## The page

`/api/starred` fetches the full starred list live from GitHub on each load
(so newly-starred repos and fresh push dates appear immediately) and merges in
each repo's cached blurb, falling back to the repo's GitHub description for any
repo not yet cached. Repo links are scheme-guarded (`safeHref`) and every cell is
set via `textContent`, never `innerHTML`.

**The live list has a cached fallback.** The star list is the one thing on this
page fetched live, and GitHub paginates it — up to 10 sequential requests at a
15s timeout each. A slow or rate-limited API used to blank the page entirely,
even though three caches on disk already held most of what it renders. So the
list is cached too, in `config/starred_repos.json`, written by
`tasks/starred_releases.py` (which already walks the live list nightly, so this
needs no extra job). When the live fetch fails, `_repo_list` serves that cache
and the response carries `stale: true` and `fetched_at`; the page shows a
"GitHub is unreachable — showing the cached list from …" note above the table.
Only when *both* the live fetch and the cache come up empty does the page show
the error instead — a first run, or a fresh checkout.

Reachable from the nav on the dashboard, opportunities, memories, and system-map
pages.

## The blurbs

`tasks/starred_blurbs.py` generates the blurbs, cached in
`config/starred_blurbs.json` keyed by `full_name`. For each repo it makes **one
isolated model call**: it fetches that repo's README (`fetch_readme`), truncates
it to `README_CHARS` (2000), and asks the model for a single plain sentence. The
model only ever sees one repo's README at a time, so the context window can't
overflow no matter how many repos are starred — the star count drives the number
of calls, not the size of any prompt. If a README is missing or the model
returns nothing usable, the blurb falls back to the repo's GitHub description, so
every repo still gets a usable line.

Blurbs are generated **once per repo** and reused thereafter (a repo's purpose is
stable, and READMEs churn on every commit). De-starred repos are pruned from the
store on each run. Pass `--refresh` to regenerate every blurb:

```
python -m tasks.starred_blurbs           # only newly-starred repos
python -m tasks.starred_blurbs --refresh # regenerate all
```

## When does it run?

Weekly, Sundays at 8:00 PM, via launchd
(`launchd/local.wren.starredblurbs.plist`) — a weekly cadence is
enough to pick up newly-starred repos since each run only summarizes the ones
without a cached blurb. The model backend follows the global default (local
Ollama); override just this task with `WREN_STARRED_BLURBS_BACKEND` (see
`docs/llm-backend.md`).

## Release awareness

The "Latest release" column answers the version-level question the user actually
cares about — *"has this repo cut a new release?"* — without the noise of every
push. `tasks/starred_releases.py` fetches each starred repo's **latest published
release** (`fetch_latest_release`, one GitHub call per repo, fanned over a small
thread pool) and caches `{tag, name, published_at, html_url}` in
`config/starred_releases.json` keyed by `full_name`. `/api/starred` merges it in
and flags `release_is_new` when the release was published within the last 30 days
(`RECENT_RELEASE_DAYS`), which the page renders as a **🆕 new** badge.

Only *releases* count, not bare tags — a repo that doesn't publish releases simply
shows no version. The whole cache is rewritten from the live star list each run
(no per-repo skip: a blurb is stable but the latest release changes), so
de-starred repos are pruned automatically.

Runs **daily** at 8:00 PM via
`launchd/local.wren.starredreleases.plist` — daily, unlike the
weekly blurbs, so a new version shows up promptly. No model, so no backend knob.

```
python -m tasks.starred_releases
```

**Boundary.** This surfaces new *upstream* versions. Whether the user has a repo
installed — and at what version — is tracked separately (see below); Wren still
never runs an upgrade (`brew upgrade` / `git pull` stay the user's to run). This page
is the *awareness* layer, not an installer.

## Installed-version tracking

The "Installed" column answers the other half of the question — *"and am I behind
it?"* Only the repos the user actually has installed are tracked, and only because they
lists them: `config/starred_installed.json` (hand-edited, gitignored) maps a repo
`full_name` to how to read its installed version. Each entry is one of three shapes:

```json
{
  "rtk-ai/rtk":        {"version_cmd": "rtk --version"},
  "mattpocock/skills": {"plugin": "mattpocock-skills@claude-plugins-official"},
  "some/thing":        {"version": "v1.1.0"}
}
```

- **`version_cmd`** — a command Wren runs to read the current version. The daily
  task runs it (no shell, split with `shlex`, 10-second timeout) and extracts the
  first version-looking token from its output (stdout *or* stderr). The command
  string comes from the user's own config, not from any model or web content.
- **`plugin`** — a Claude Code plugin, named by its fully qualified
  `<plugin>@<marketplace>` key. The version is read straight out of
  `~/.claude/plugins/installed_plugins.json`, the record Claude Code writes when
  it installs or updates a plugin (honouring `CLAUDE_CONFIG_DIR` if that is set).
  Nothing is executed, so there is no `PATH` to go wrong and no timeout to hit —
  prefer this wherever a starred repo is consumed as a plugin. That file belongs
  to another program, so every step of the lookup is shape-checked: an unexpected
  schema degrades to an error string, never a traceback.
- **`version`** — a static version the user maintains by hand. The last resort,
  for anything with neither a version command nor an installer record. It goes
  stale silently — nothing can detect that a hand-typed number is wrong — so
  reach for one of the two above first.

`tasks/starred_installed.py` resolves every entry and caches
`{version, source, error, checked_at}` in `config/starred_installed_versions.json`
keyed by `full_name`, so `/api/starred` reads a plain store and never runs a
subprocess on the request path. `/api/starred` then merges it in and computes
`update_available` with `compare_versions`, which normalizes both the installed
string and the latest release tag to a numeric core (so `0.43.0` compares against
`v0.43.0`, and `skill-v4.0.2` against `skill-v4.1.0`) and parses them with
`packaging.version`. It is deliberately conservative: an unparseable or missing
version on either side yields *no* badge rather than a false "update available".

The page renders the installed version, an amber **update available** badge when
it's behind the latest release, and a muted **⚠ check failed** (with the error as
its tooltip) when a `version_cmd` didn't resolve. Repos with no entry simply show
a blank cell — the column is opt-in per repo.

A failed check does **not** erase a version an earlier run measured. The cache is
rewritten whole each run, so one bad run — a tool briefly missing from `PATH`,
say — would otherwise blank every good value at once. The last known version is
carried over with `"stale": true` beside the error, and the page renders it with
a muted **⚠** whose tooltip names the failure, so a carried-over number never
reads as a fresh measurement. The flag clears as soon as the command works again.

`config/starred_installed.example.json` is a copy-and-edit starting point. The
whole cache is rewritten from the source each run, so removing a repo from the
config prunes it. Runs **daily** at 8:10 PM via
`launchd/local.wren.starredinstalled.plist`. The plist sets a
`PATH` covering Homebrew/Cargo/`~/.local/bin` because launchd's default `PATH` is
minimal; a tool outside those dirs can be given by absolute path in the config.
The dashboard's **Run now** does not read that plist — it spawns the task from
the chat server, whose own plist sets no `PATH` — so `chat/insights.py:_child_env`
prepends the same bin dirs to every run-now child. Without it, on-demand runs
failed with `FileNotFoundError` on the very commands the 8:10 PM run resolved
fine, and (before the carry-over above) blanked the whole column doing it.
No model, so no backend knob.

```
python -m tasks.starred_installed
```

## Limitations

Even with the README, the small local model won't always match a hand-written
blurb — it can over-compress a marketing-heavy README. The blurbs are noticeably
richer than the bare GitHub description, but treat them as a helpful gist, not a
canonical summary.
