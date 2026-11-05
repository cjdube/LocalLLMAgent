# /wiki/review — deciding what to cut from the learnings vault

The lint finds what is broken. It cannot tell you that a whole topic stopped
mattering. `/wiki/review` shows the whole vault, sorted into plain-language
groups, so one choice covers many pages.

## How to reach it

Press **Review vault** in the toolbar on `/wiki/lint`. That is the only way in.
The page has no menu item on purpose: the menu is long enough, and the review is
the same job as the lint, which is cleaning the vault. `nav.js` knows nothing
about the page, so no menu item shows as active there.

## What it does

Mark a group **Keep**, **Not sure** or **Don't care**. Open a group to override
one page with its tick box. Marks save as you go. **Copy cut list** copies the
names of every page marked to cut, one per line.

**It writes nothing to the vault.** The marks are saved in
`config/wiki_review.json`. Cutting pages remains a separate, manual job, because
a safe cut must also flatten links, remove index lines and add a `RULES.md`
bullet.

## Where the data comes from

`chat/wikireview.py` reads the vault on every page load, so the facts are never
stale. The group list is stored data in `config/wiki_review.json`, which is
gitignored because it holds real page names.

| Store field | Holds |
| --- | --- |
| `groups` | Each group's `id`, `family`, `title` and `line`, plus either a `pages` list or a dated `prefix` |
| `marks` | Group id to `{verdict, pages}`, where `pages` holds the per-page overrides |
| `sorted_at` | The date the group list was last sorted |

The stored list and the vault are matched on each read:

- A page goes to the first group that claims it, so it is never in two groups.
- A dated page goes to the group whose `prefix` matches the name before its date.
- A page that no group claims shows in **Unsorted**. New pages land there.
- A name that left the vault drops out. A group left with no pages is hidden.

Nothing sorts new pages. A local model failed that test: see below.

The inbound-link count uses `link_targets` from `chat/wikigraph.py`, so a link
inside a code span does not count, and `/wiki` and this page agree. It folds
case, because Obsidian resolves links that way. Summaries keep 220 characters,
not the graph's 140, because this page needs enough text to judge a page.

## API

Both routes need a signed-in session, like the rest of `chat/routes_wiki.py`.

- `GET /api/wiki/review` returns `{groups, pages, marks, sorted_at}`. `pages`
  maps a name to `[summary, words, sources, inbound, newest date]`. A missing
  vault returns 404 with `{"error": ...}`.
- `PUT /api/wiki/review/marks/<group_id>` takes `{verdict, pages}`. A verdict is
  `keep`, `unsure`, `cut` or null, and a page mark is `keep` or `cut`. A bad value
  returns 400. An unknown group, or a page that is not in that group, returns 404.

Page summaries come from web sources, so they are untrusted. Every string from
the API goes through `esc()` in `chat/static/wiki-review.js`.

## Can a local model sort new pages?

Not well enough. `evals/sort_eval.py` tests this against the hand sort. Each call
sends the 29 numbered topic groups and 40 numbered pages, and the model replies
with number pairs. The key is a path, because it holds private page names:

```
.venv/bin/python -m evals.sort_eval --key config/wiki_review.json --models gemma4:26b-mlx qwen3.8:27b-mlx
```

Run of 2026-11-05: 636 topic pages, 3 trials per model, production settings.

| | Placed after retry | Match with hand sort | One trial |
| --- | --- | --- | --- |
| gemma4:26b-mlx | 636/636 | 64.2%, 67.3%, 66.5% | about 73 s |
| qwen3.8:27b-mlx | 636/636 | 66.7%, 66.0%, 64.6% | about 222 s |

The pass mark was all pages placed, at least 80% match, and at most 3 real
mistakes in 30 disagreements read by hand. Placement passed, and the other two
failed. Of the 30 read, one reader judged 11 real mistakes and 19 where both
groups were fine (Gemma 7 of 14, qwen 4 of 16). So the match number understates
the model, because many pages fit two groups. The mistakes still matter: a page
in the wrong group can reach the cut list through a group's **Don't care** mark.

The model sees only a name and a summary. Some of the real mistakes put one of
the user's own projects in a general group, because a summary does not say whose
project it is. A sort that used more than that would be a different test.
