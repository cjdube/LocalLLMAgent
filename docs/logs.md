# Log viewer (`/logs`)

A formatted reader for everything under `logs/`. Pick a file, filter by level or
text, and read it as grouped entries rather than as raw lines.

## Why it exists

The dashboard's run drawer already showed *scheduled task* runs, but it answers a
different question — "did the job work?" — and it refuses daemons outright ("the
chat server runs continuously; it has no discrete scheduled runs"). That left
`logs/wren.log`, the biggest log and the one you want while testing a chat
change, with no way in short of ssh.

## Order and the live tail

**Newest first.** The latest line is at the top; scrolling down goes back in
time, and `load older` appends the next page at the bottom.

The API returns entries in *chronological* order regardless — pairing a run's
start with its end and attaching continuation lines to the line above them are
both natural forwards and fiddly backwards, so the reversal happens in
`renderStream` at the last moment. Only that one function thinks in two
directions.

**Live** (the checkbox, on by default) polls every 4 seconds for whatever has
been appended and puts it on top. It carries a byte cursor (`next_after`), so a
poll transfers only new bytes, not the page again. Three details worth knowing:

- A line still being written is **not** read until its newline lands. Otherwise
  the tail shows a fragment and the next poll renders the same line a second
  time.
- If more than a window's worth arrived while you weren't looking — a burst, or
  a Mac that slept — the catch-up read stays bounded and reports `skipped`
  rather than growing to match the gap.
- Scroll position is held steady when entries arrive, so reading something
  further down doesn't get yanked around.

Polling stops when the browser tab is hidden, and when the box is unchecked.

## What it shows

- **One entry per timestamped line**, with its continuation lines folded
  underneath it. Roughly 31% of lines in these logs are continuations — a
  drafted digest under one `INFO`, a traceback under one `ERROR` — so a
  line-per-row reader shreds them.
- **A severity rail** in the left gutter, plus a tinted row for warnings and
  errors. Across the whole corpus there are ~6,300 `INFO` lines against 80
  `WARNING` and 12 `ERROR`, so the eye needs somewhere to land.
- **The date on a day divider, the time on the row.** Every line otherwise opens
  with the same 23-character stamp.
- **Highlighting for the two shapes the loggers emit**: `key=value` runs
  (`model=gemma4:26b-mlx prompt_tokens=5362`) and `name(args) -> result`.
- **Run grouping** where the log has runs — one header above each run block
  carrying its duration (`run 05:15:00 · 4.4s`), or `· running` for one still
  open. Boundaries come from the same predicates `chat/insights.py` uses for the
  dashboard, so the two views agree.
- **A density bar** over the scanned window, one cell per bucket, coloured by the
  worst level in it.

## Filtering

`all levels` / `warnings and errors` / `errors only`, plus a text filter that
searches continuation lines as well as the message.

A filtered view keeps **two rows of context either side of each hit**, dimmed.
That is not decoration. On 2026-07-10 a `WARNING` reported that the prompt had
overflowed `num_ctx` and truncated the system prompt; the cause — a 46,683-char
calendar tool result — is three lines above it. A filter showing bare matches
reports symptoms and hides causes.

## The two streams

Every task writes two files, and they are not redundant:

| Stream | File | What it is |
| --- | --- | --- |
| structured log | `logs/<key>.log` | `setup_logger`'s file handler. Rotated at 2 MB, 3 backups. |
| launchd stdout | `logs/<key>.launchd.log` | launchd's `StandardOutPath`. Where a crash *before* the logger initialises lands. |

Files in `logs/` that no task claims (a retired task's log) are listed after the
tasks. `*.log.bak` files and `logs/archive/` are not listed — nothing appends to
them, so a reader built around "tail the end" has nothing to offer them.

## Bounds

Reads are bounded, and the reason is the second stream above. The `.log` files
are capped at 8 MB by their `RotatingFileHandler`, but the `.launchd.log` files
are append-only and rotated by nothing — `wren.launchd.log` grows about 19 KB a
day. So a read seeks backwards over a fixed 512 KB window from the end of the
file (or from the paging cursor) rather than loading it, and `load older` pages
further back.

Because of that, the counts under the filter bar describe **the window that was
read**, not the file — the caption says which, and says "whole file" only when
the scan reached byte 0.

Two per-entry caps, both reporting what they dropped rather than swallowing it:
a message is cut at 4,000 characters and a continuation block at 200 lines. The
longest line on record is 46,683 characters.

The viewer reads the **live file only**, not the rotated `.log.1`/`.2`/`.3`
siblings that `insights._read_lines` stitches together. A byte offset is the
paging cursor, and a cursor spanning several files would have to become a
`(file, offset)` pair — real complexity for a case that has not occurred yet
(nothing has reached the 2 MB rotation threshold). The file picker reports how
many rotated siblings exist so the page can say older data is there.

## Safety

Log content is untrusted: it carries fetched page titles, URLs, and model
output. Everything reaches the DOM through `textContent`, and the highlighter
builds spans as nodes rather than as markup strings — there is no `innerHTML` in
`chat/static/log-view.js`.

No endpoint takes a path. A request names a *key*, which is looked up in the
catalogue `chat/logview.py` builds or 404s, so a key naming a file outside
`logs/` is unknown rather than a traversal to defend against.

## Layout

| File | Role |
| --- | --- |
| `chat/logview.py` | Catalogue, bounded reads, entry folding, filtering. No Flask. |
| `chat/routes_logs.py` | `GET /api/logs`, `GET /api/logs/entries` |
| `chat/static/logs.html` | The page and its CSS |
| `chat/static/log-view.js` | Rendering, highlighting, folding, paging |

`chat/logview.py` runs standalone, like `chat/insights.py`:

```bash
.venv/bin/python -m chat.logview          # list readable logs
.venv/bin/python -m chat.logview wren     # dump the tail of one
```
