# The starred view — how it works

The `/starred` view lists Craig's starred GitHub repos as a table — **Repo ·
Language · What it does · Last updated** — sorted by most-recently-pushed. The
"what it does" blurb is written by the local model from each repo's README and
cached, so the page loads instantly and the model never sits on the request
path.

Code: `chat/static/starred.html` + the `/starred` and `/api/starred` routes in
`chat/server.py` (the view), `tasks/starred_blurbs.py` (the cached-blurb job),
and `fetch_starred_repos` / `fetch_readme` in `agent/tools/github_starred.py`
(the GitHub data). Needs `GITHUB_TOKEN` (already used by the morning brief).

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

## Limitations

Even with the README, the small local model won't always match a hand-written
blurb — it can over-compress a marketing-heavy README. The blurbs are noticeably
richer than the bare GitHub description, but treat them as a helpful gist, not a
canonical summary.
