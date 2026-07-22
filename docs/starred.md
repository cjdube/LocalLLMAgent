# The starred view — how it works

The `/starred` view lists Craig's starred GitHub repos as a table — **Repo ·
Language · What it does · Latest release · Last updated** — sorted by
most-recently-pushed. The "what it does" blurb is written by the local model from
each repo's README and cached; the "latest release" column is filled from a
separate cache. Both are precomputed by scheduled tasks, so the page loads
instantly and the model never sits on the request path.

Code: `chat/static/starred.html` + the `/starred` and `/api/starred` routes in
`chat/server.py` (the view), `tasks/starred_blurbs.py` (the cached-blurb job),
`tasks/starred_releases.py` (the cached-release job), and `fetch_starred_repos` /
`fetch_readme` / `fetch_latest_release` in `agent/tools/github_starred.py` (the
GitHub data). Needs `GITHUB_TOKEN` (already used by the morning brief).

## The page

`/api/starred` fetches the full starred list live from GitHub on each load
(so newly-starred repos and fresh push dates appear immediately) and merges in
each repo's cached blurb, falling back to the repo's GitHub description for any
repo not yet cached. If the GitHub fetch fails (rate limit, bad token) the page
shows the error rather than crashing. Repo links are scheme-guarded
(`safeHref`) and every cell is set via `textContent`, never `innerHTML`.

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
(`launchd/com.craigdube.localllmagent.starredblurbs.plist`) — a weekly cadence is
enough to pick up newly-starred repos since each run only summarizes the ones
without a cached blurb. The model backend follows the global default (local
Ollama); override just this task with `WREN_STARRED_BLURBS_BACKEND` (see
`docs/llm-backend.md`).

## Release awareness

The "Latest release" column answers the version-level question Craig actually
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
`launchd/com.craigdube.localllmagent.starredreleases.plist` — daily, unlike the
weekly blurbs, so a new version shows up promptly. No model, so no backend knob.

```
python -m tasks.starred_releases
```

**Boundary.** This surfaces new *upstream* versions. It does **not** know whether
Craig has a repo installed (via Homebrew or a local clone), and Wren never runs an
upgrade — `brew upgrade` / `git pull` stay Craig's to run. Homebrew is already the
update mechanism for brew-installed software (`brew outdated` / `brew upgrade`);
this page is the *awareness* layer, not an installer.

## Limitations

Even with the README, the small local model won't always match a hand-written
blurb — it can over-compress a marketing-heavy README. The blurbs are noticeably
richer than the bare GitHub description, but treat them as a helpful gist, not a
canonical summary.
