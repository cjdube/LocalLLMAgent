"""Read/write the Weekly Learning & Project Log Google Doc.

Ported from ai-memory's update_weekly_log.py, adapted to use the shared
OAuth helper (agent/tools/google_auth.py) instead of a standalone credentials
file, and to return dicts rather than print/exit — this module is called
directly by tasks/weekly_learnings.py, not exposed as an agent tool_call.

Usage:
    python -m agent.tools.docs read-previous
    python -m agent.tools.docs insert --file /path/to/entry.txt
"""

import argparse
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from agent.tools.google_auth import build_service

_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_ROOT / "config" / ".env")

ANCHOR_TEXT = "<insert new entries after this line>"
SECTION_HEADING = "Strategic Weekly Review"


def _doc_id() -> str:
    doc_id = os.getenv("WEEKLY_LOG_DOC_ID")
    if not doc_id:
        raise RuntimeError("WEEKLY_LOG_DOC_ID not set in config/.env")
    return doc_id


def _paragraph_text(element: dict) -> str:
    paragraph = element.get("paragraph")
    if not paragraph:
        return ""
    return "".join(pe.get("textRun", {}).get("content", "") for pe in paragraph.get("elements", []))


def get_previous_entry_text(max_chars: int = 4000) -> str:
    """Return the text of the most recent '## Strategic Weekly Review' block,
    for carry-forward context. Returns '' if the doc/section can't be read."""
    try:
        service = build_service("docs", "v1")
        doc = service.documents().get(documentId=_doc_id()).execute()
    except Exception:
        return ""

    body = doc.get("body", {})
    lines = [_paragraph_text(e) for e in body.get("content", [])]

    start_idx = None
    for i, line in enumerate(lines):
        if SECTION_HEADING in line:
            start_idx = i
            break
    if start_idx is None:
        return ""

    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        if SECTION_HEADING in lines[i]:
            end_idx = i
            break

    text = "".join(lines[start_idx:end_idx]).strip()
    return text[:max_chars]


def _find_insertion_index(service, doc_id: str) -> int:
    doc = service.documents().get(documentId=doc_id).execute()
    body = doc.get("body", {})
    for element in body.get("content", []):
        text = _paragraph_text(element)
        if ANCHOR_TEXT in text:
            return element["endIndex"]
    raise RuntimeError(f"Anchor text not found in document: {ANCHOR_TEXT!r}")


def _parse_bold(text: str):
    parts, ranges = [], []
    pos = raw_pos = 0
    for m in re.finditer(r"\*\*(.+?)\*\*", text):
        before = text[raw_pos:m.start()]
        parts.append(before)
        pos += len(before)
        bold_start = pos
        content = m.group(1)
        parts.append(content)
        pos += len(content)
        ranges.append((bold_start, pos))
        raw_pos = m.end()
    parts.append(text[raw_pos:])
    return "".join(parts), ranges


def _parse_blocks(raw_content: str):
    blocks = []
    for raw_line in raw_content.splitlines():
        stripped = raw_line.strip()
        if stripped == "---":
            pass
        elif stripped.startswith("## "):
            clean, bold_ranges = _parse_bold(stripped[3:])
            blocks.append({"style": "HEADING_2", "text": clean, "bullet": False, "bold_ranges": bold_ranges})
        elif stripped.startswith("### "):
            clean, bold_ranges = _parse_bold(stripped[4:])
            blocks.append({"style": "HEADING_3", "text": clean, "bullet": False, "bold_ranges": bold_ranges})
        elif stripped.startswith("- "):
            clean, bold_ranges = _parse_bold(stripped[2:])
            blocks.append({"style": "NORMAL_TEXT", "text": clean, "bullet": True, "bold_ranges": bold_ranges})
        elif stripped == "":
            blocks.append({"style": "NORMAL_TEXT", "text": "", "bullet": False, "bold_ranges": []})
        elif blocks and blocks[-1]["bullet"] and raw_line[:1] in (" ", "\t"):
            prev = blocks[-1]
            offset = len(prev["text"])
            clean_cont, bold_ranges_cont = _parse_bold(" " + stripped)
            prev["text"] += clean_cont
            prev["bold_ranges"] += [(s + offset, e + offset) for s, e in bold_ranges_cont]
        else:
            clean, bold_ranges = _parse_bold(stripped)
            blocks.append({"style": "NORMAL_TEXT", "text": clean, "bullet": False, "bold_ranges": bold_ranges})
    return blocks


def insert_weekly_entry(content: str) -> dict:
    try:
        service = build_service("docs", "v1")
        doc_id = _doc_id()
        insert_index = _find_insertion_index(service, doc_id)
        blocks = _parse_blocks(content)

        full_text = "\n".join(b["text"] for b in blocks) + "\n"
        service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"insertText": {"location": {"index": insert_index}, "text": full_text}}]},
        ).execute()

        doc = service.documents().get(documentId=doc_id).execute()
        body = doc.get("body", {})

        format_requests = []
        block_idx = 0
        for element in body.get("content", []):
            if block_idx >= len(blocks):
                break
            paragraph = element.get("paragraph")
            if not paragraph:
                continue
            start = element.get("startIndex", 0)
            end = element.get("endIndex", 0)
            if start < insert_index:
                continue

            block = blocks[block_idx]
            format_requests.append({
                "updateParagraphStyle": {
                    "range": {"startIndex": start, "endIndex": end},
                    "paragraphStyle": {"namedStyleType": block["style"]},
                    "fields": "namedStyleType",
                }
            })
            if block["bullet"]:
                format_requests.append({
                    "createParagraphBullets": {
                        "range": {"startIndex": start, "endIndex": end},
                        "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                    }
                })
            for bold_start, bold_end in block["bold_ranges"]:
                format_requests.append({
                    "updateTextStyle": {
                        "range": {"startIndex": start + bold_start, "endIndex": start + bold_end},
                        "textStyle": {"bold": True},
                        "fields": "bold",
                    }
                })
            block_idx += 1

        if format_requests:
            service.documents().batchUpdate(documentId=doc_id, body={"requests": format_requests}).execute()
    except Exception as e:
        return {"error": str(e)}

    return {"inserted": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("read-previous")
    p_insert = sub.add_parser("insert")
    p_insert.add_argument("--file", required=True)

    args = parser.parse_args()
    if args.cmd == "read-previous":
        print(get_previous_entry_text())
        return 0
    else:
        content = Path(args.file).read_text()
        result = insert_weekly_entry(content)
        print(result)
        return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
