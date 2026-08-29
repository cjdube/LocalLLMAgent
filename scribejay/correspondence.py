"""Yesterday's sent mail, turned into a record of who was written to.

The record says what he built (scribejay/daily_commits.py), what he read
(daily_chrome_learnings) and where his hours went (AI Session Time Blocks). None of it
says who he talked to. This is the only source that does.

**Metadata only.** `agent.tools.gmail_read.fetch_sent_metadata` asks Gmail for
headers and nothing else, so the bodies are never fetched rather than fetched and
discarded. What is recorded is who, when, about what subject line, and whether it
was an answer or a decision to reach out.

**No model call**, and not for want of one: with no bodies, the model would have
only a subject and some names, and any sentence richer than those is invented. So
the page is assembled in Python, the way scribejay/strava_download.py maps Strava
fields onto calendar events without a natural-language step. The subject line the
user wrote is the most accurate description of the exchange that exists.
"""

import os
from email.utils import getaddresses
from pathlib import Path

DEFAULT_CORRESPONDENCE_DIR = str(Path.home() / "Vaults" / "llm-wiki-learnings" / "correspondence")

# Subjects that are machinery rather than correspondence. Everything Wren sends
# herself is already caught by the self-addressed rule below; this list is for
# mail to a real outside address that still is not a conversation. Expected to
# grow — a missed one costs a junk line in a page, not a failure.
NOISE_SUBJECTS = ("unsubscribe",)


def _correspondence_dir() -> Path:
    """Where the pages go. **Not** LEARNINGS_DIR.

    LEARNINGS_DIR is the vault's `raw/`, ObsidianWikiAgent's ingest queue, and
    anything dropped there becomes an asserted wiki page — which would turn the
    people he emails and the companies they work for into wiki entities. A record
    of who he wrote to is a diary, so it gets its own directory outside the queue,
    the way daily_synthesis archives nudges to SYNTHESIS_DIR."""
    return Path(os.getenv("CORRESPONDENCE_DIR", DEFAULT_CORRESPONDENCE_DIR)).expanduser()


def _recipients(row: dict) -> list:
    """Every address on To and Cc, lowercased. Parsed with the stdlib's own header
    parser rather than split on commas: a display name may contain one."""
    return [addr.lower() for _, addr in getaddresses([row.get("to", ""), row.get("cc", "")])
            if addr]


def _people(row: dict, me: str) -> dict:
    """The people on one message as {address: label}, never including the user.

    Keyed on the address because the same person arrives named on one message and
    bare on the next — Diego was on one thread as both "Diego M. Oppenheimer" and
    diego@hq.aitinkerers.org, and a set of strings listed him twice."""
    out = {}
    for name, addr in getaddresses([row.get("to", ""), row.get("cc", "")]):
        if not addr or addr.lower() == me:
            continue
        label = (name or "").strip().strip('"')
        out[addr.lower()] = label or addr
    return out


def _merge_people(into: dict, more: dict) -> None:
    """Fold one message's people into a thread's, preferring a real name over a
    bare address whichever order the two arrive in."""
    for addr, label in more.items():
        if addr not in into or into[addr] == addr:
            into[addr] = label


def filter_noise(rows: list, me: str, logger=None) -> list:
    """Drop what is machinery rather than correspondence.

    Two rules, and the first is the one that matters: **mail addressed only to
    himself is not correspondence.** That is every Morning Brief, opportunity
    digest and log-inspector rollup Wren sends — 4 of 10 sent messages in a
    sample fortnight, which is why a filter is the first thing this needs rather
    than a refinement to add later."""
    me = (me or "").lower()
    kept = []
    for row in rows:
        addresses = _recipients(row)
        if not addresses or all(a == me for a in addresses):
            continue
        if (row.get("subject") or "").strip().lower() in NOISE_SUBJECTS:
            continue
        kept.append(row)
    if logger and len(kept) < len(rows):
        logger.info(f"filtered {len(rows) - len(kept)} of {len(rows)} sent message(s) "
                    "as self-addressed or machine mail")
    return kept


def group_threads(rows: list, me: str) -> list:
    """One row per conversation, in the order it was first written to.

    Grouped on Gmail's own thread id, so four messages over an evening read as one
    exchange rather than four. `reached_out` is taken from the FIRST message of
    the day on the thread: answering is routine, and deciding to start something
    is the part of a day worth recording."""
    threads: dict = {}
    for row in rows:
        key = row.get("thread_id") or row.get("message_id")
        thread = threads.get(key)
        if thread is None:
            threads[key] = {
                "thread_id": key,
                "subject": row.get("subject") or "(no subject)",
                "people": _people(row, me),
                "messages": 1,
                "reached_out": not row.get("is_reply"),
                "first": row.get("date", ""),
            }
            continue
        thread["messages"] += 1
        _merge_people(thread["people"], _people(row, me))
    return list(threads.values())


def _subject_label(subject: str) -> str:
    """The subject without Gmail's reply prefix — the thread is already marked as
    a reply by which section it lands in, so "Re:" would say it twice."""
    label = subject.strip()
    while label[:3].lower() == "re:":
        label = label[3:].strip()
    return label or "(no subject)"


def render_page(threads: list, day) -> str:
    """The whole page, in Python. See the module docstring for why no model runs
    over this: with no bodies, anything beyond the subject and the names would be
    invented."""
    reached = [t for t in threads if t["reached_out"]]
    replied = [t for t in threads if not t["reached_out"]]

    def block(title: str, rows: list) -> list:
        lines = [f"### {title}"]
        if not rows:
            lines.append("- **None:** [No qualifying items for this section]")
            return lines
        for t in rows:
            people = ", ".join(t["people"].values()) or "(no recipient)"
            count = f" ({t['messages']} messages)" if t["messages"] > 1 else ""
            lines.append(f"- **{_subject_label(t['subject'])}** — {people}{count}")
        return lines

    return "\n".join(
        [f"## Correspondence: {day:%B %-d, %Y}", ""]
        + block("Reached out", reached) + [""]
        + block("Replied", replied) + [""]
    )
