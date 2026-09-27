# Uploaded files

Wren can read a CSV file you give her in chat. Tap the paperclip beside the
message box, pick a `.csv`, and the page uploads it and fills the box with
`I uploaded <name>. ` — finish the sentence with your question and send.

This is the general "give Wren a file" capability. Subscription tracking (a
later phase) builds on it; nothing here knows what the file contains.

## What happens to the file

- `POST /api/imports` (`chat/routes_imports.py`, login required) hands the bytes
  to `agent/tools/imports.py:save_import()`.
- It must be a `.csv` of at most 5 MB. The limit is raised for this one
  blueprint only — every other route keeps the app-wide 256 KB cap.
- The text is decoded as UTF-8 (falling back to Latin-1, which is what many bank
  exports use), the delimiter is sniffed (`,` `;` tab `|`), and the file is
  re-written as plain UTF-8 comma CSV under `config/imports/<random id>.csv`.
  The name you uploaded is kept as a label in `config/imports.json` only — it
  never becomes a path.
- The newest 20 uploads are kept. The 21st upload deletes the oldest file and
  its row.
- Both `config/imports/` and `config/imports.json` are gitignored: they hold
  personal data.

## What Wren can do with it

Two read-only tools in the deferred `files` group ([tool-loading.md](tool-loading.md)):

- `list_imports` — the uploaded files, newest first, numbered (`1` = newest).
  The model gets numbers, never the stored ids
  ([opaque-identifiers.md](opaque-identifiers.md)).
- `read_import(n, start_row)` — one page of rows, at most 100 rows or about 6,000
  characters. Every page says "Showing rows X–Y of Z", and a page that is not
  the last one says it is **not** the whole file and gives the next `start_row`.
  Rows count from 1, as people count them: a 0-based offset made the model
  show rows 51–56 when asked for "rows 50 to 55", 3 of 3 times.

The group pre-loads when a message says "upload", "csv", "spreadsheet", "the
file", "my file" and similar. A bare "import" or "file" is deliberately not a
cue — they fire on "important" and "file a ticket".

The small model reads a page at a time. It is fine for "what's in this file?"
or "show me the rows for March"; it is **not** fit to scan thousands of rows
and total them. Whole-file analysis belongs in Python, the way subscription
detection will do it.

## Privacy

Uploads stay on the Mac and are read by the local model. But the chat's
**Redo with …** button and the busy-slot **Ask …** offer send the conversation
to the cloud backend (`WREN_ESCALATION_BACKEND`) — including any rows Wren has
read in that chat. They only fire on your tap. Start a **New chat** first if you
want to escalate something unrelated.

## Removing a file

There is no delete button yet. From the Mac:

```bash
.venv/bin/python -m agent.tools.imports --list
.venv/bin/python -m agent.tools.imports --delete 1
```

`--read N [--start-row K]` prints a page, and `--add PATH` imports a local file
without the browser.
