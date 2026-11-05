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

Nothing sorts new pages yet. Whether Gemma can do that is an open question.

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
