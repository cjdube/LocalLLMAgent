---
description: Summarize what changed in the user's starred GitHub repos over a recent window.
---

When the user asks what's new in the repos they star (e.g. "what changed this week"):

1. Call `fetch_starred_repos` with `days_ago` set to the window he named — pass
   the number of days, don't compute a date yourself.
2. For each repo with activity, lead with the repo name and condense its
   `recent_changes`. If a repo's changes end with a "(+N more commits)" or
   "(+N more releases)" note, carry that count into your summary (e.g. "plus 8
   more commits") — it signals the repo had more going on than what's shown.
3. Skip repos with no recent activity. If nothing changed in the window, say so
   plainly rather than padding.
