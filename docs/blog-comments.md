# Blog comments push

`tasks/blog_comments.py` runs daily at 8:00 AM (`launchd/local.wren.blogcomments.plist`).
It asks the blog's Cloudflare D1 database how many reader comments wait for
approval. If any do, it sends one ntfy push with the count per post and a
**Moderate** button to `https://blog.craigdube.dev/admin/comments`. If none do,
it stays silent.

## Why Wren polls, not the blog

The blog runs on Cloudflare. The ntfy server is reachable only over Tailscale,
so Cloudflare cannot push to it. Opening ntfy to the internet (Tailscale Funnel)
was rejected. So Wren reads D1 through the Cloudflare REST API, and the blog
repo needs no change.

The blog's own `/api/admin/comments` route is not used: it sits behind
Cloudflare Access and would need a separate service token.

## Behaviour

- **No "already notified" state.** Every run reports the full pending count, so
  a comment left alone nags again the next day. No state file.
- **Counts and slugs only.** Comment names and bodies are text from strangers.
  The query does not select them, so they never reach the phone.
- **Failure** (missing setting, HTTP error, `success: false`) goes through
  `notify_failure`, like every other task.
- **Push down:** `email_fallback=True` emails the same line instead, because this
  push fires once and nothing retries it.

## Setup

| Key | Where | What |
|---|---|---|
| `CF_D1_TOKEN` | `config/.env` (secret) | Cloudflare **User API Token**, custom, with *Account → D1 → Read* only, on one account. Read is enough for the `/query` endpoint's `SELECT`. |
| `CF_ACCOUNT_ID` | `/settings` → Blog | The Cloudflare account ID. |
| `CF_D1_DATABASE_ID` | `/settings` → Blog | The ID of the `blog-comments` D1 database. |

Run it once by hand:

```bash
.venv/bin/python -m tasks.blog_comments
```
