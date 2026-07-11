"""Gather fractional-work opportunity signals and email the daily Opportunity
Digest. Non-interactive — run by launchd each morning.

Three signals from free, ToS-clean sources (see CLAUDE.md's data sourcing
policy — no LinkedIn scraping, no paid data SaaS):
  - funded: new SEC Form D filings in New England (EDGAR full-text search API)
  - hiring: product/eng leadership openings on watched companies' public
    Greenhouse/Lever/Ashby boards, plus HN "Who is hiring" posts
  - stalled_search: a watched leadership opening still unfilled after
    OPP_STALLED_DAYS — the founder-burnout signal worth a peer outreach

The pollers and the dedupe store do everything deterministic; the local LLM
only scores each NEW item 1-10 for fractional-operator fit and drafts a
one-line outreach angle. HTML is assembled in Python (morning-brief pattern).
Only items not seen before (or newly stalled) are reported, so an empty day
sends no email.

The whole pipeline lives in build_and_send_digest(), shared by this scheduled
task (main, below) and the chat server's send_opportunity_digest tool.

Usage:
    python -m tasks.opportunity_digest
"""

import html
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

import requests

from agent.loop import complete_text
from agent.store import atomic_write_json, load_json
from agent.tools import opportunities
from agent.tools.email import send_email
from agent.tools.notify import notify
from tasks._common import notify_failure, setup_logger, today_str

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / "config" / ".env")

STATE_PATH = _ROOT / "config" / "opportunities_state.json"

EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"

# EDGAR requires a descriptive User-Agent naming a contact; anonymous UAs get
# throttled or blocked.
_EDGAR_UA = f"Wren opportunity scout ({os.getenv('BRIEF_TO_EMAIL', 'contact unset')})"

_TIMEOUT_S = 15
# EDGAR full-text search pages are fixed at 10 hits; 5 pages per state bounds
# a runaway window (e.g. first run after a long gap) at 50 filings per state.
_EDGAR_MAX_PAGES = 5
_HN_MAX_PAGES = 3
# Cap on items sent to the local model for scoring in one run — a burst
# (first-ever run, busy Form D day) shouldn't build an unbounded prompt.
# Unscored overflow items still reach the digest, just without a score line.
MAX_SCORE_ITEMS = 40
_SNIPPET_CHARS = 300


def _states() -> list:
    return [s.strip().upper() for s in
            os.getenv("OPP_STATES", "MA,NH,ME,VT,RI,CT").split(",") if s.strip()]


def _stalled_days() -> int:
    return int(os.getenv("OPP_STALLED_DAYS", "45"))


def _score_threshold() -> int:
    return int(os.getenv("OPP_SCORE_THRESHOLD", "8"))


SCORING_SYSTEM_PROMPT = """You score business-development leads for Craig, a fractional \
product/engineering leader (Vibe Foundry) who steps into tech companies under ~100 people \
for 1-2 days a week to clear roadmaps, stabilize teams, and build AI-accelerated delivery \
workflows. You'll get a JSON list of leads. Signals mean: "funded" = the company just \
raised money (investor pressure to ship), "stalled_search" = a leadership seat has sat \
open a long time (founder is stretched thin), "hiring" = they're seeking product/eng \
leadership.

For EACH lead output exactly one line in this format, nothing else:
id|score|angle

- score: 1-10, how likely this company has a gap between product strategy and engineering \
execution that a fractional operator could fill. Investment funds, holding companies, \
banks, and real-estate vehicles score 1-2. Small operating tech/product companies with a \
fresh raise or a long-open leadership seat score high.
- angle: ONE short sentence suggesting Craig's outreach angle. Plain text, no pipe \
characters, no markdown.

Output only the id|score|angle lines, one per lead, no preamble."""


# ---- watermark -------------------------------------------------------------

def _read_edgar_watermark() -> Optional[str]:
    # Single-writer state (only this task writes it), so no locked() — atomic
    # writes alone make lock-free reads safe.
    return load_json(STATE_PATH, {}).get("edgar_window_start")


def _write_edgar_watermark(day: str) -> None:
    atomic_write_json(STATE_PATH, {"edgar_window_start": day})


# ---- pollers ---------------------------------------------------------------

# Form D is filed by every kind of exempt offering — most filings are pooled
# investment vehicles (funds, LPs), not operating startups. Skip the obvious
# ones so the scoring prompt isn't mostly noise; the model down-scores
# whatever slips through.
_FUND_NAME_RE = re.compile(
    r"\b(fund|capital|partners|holdings|realty|equity|investors|investments|"
    r"acquisition|spv|l\.?p\.?)\b",
    re.IGNORECASE,
)


def poll_edgar(start_date: str, end_date: str, states: list) -> dict:
    """New Form D filings with a principal place of business in `states`,
    filed within [start_date, end_date] (YYYY-MM-DD)."""
    headers = {"User-Agent": _EDGAR_UA}
    items = []
    try:
        for state in states:
            for page in range(_EDGAR_MAX_PAGES):
                params = {
                    "forms": "D",
                    "startdt": start_date,
                    "enddt": end_date,
                    "locationCodes": state,
                    "from": page * 10,
                }
                resp = requests.get(EDGAR_SEARCH_URL, params=params,
                                    headers=headers, timeout=_TIMEOUT_S)
                resp.raise_for_status()
                hits = resp.json().get("hits", {}).get("hits", [])
                for hit in hits:
                    src = hit.get("_source", {})
                    adsh = src.get("adsh", "")
                    names = src.get("display_names") or [""]
                    # "Acme Inc  (CIK 0001234567)" -> "Acme Inc"
                    company = re.sub(r"\s*\(CIK [0-9]+\)\s*$", "", names[0]).strip()
                    if not adsh or not company or _FUND_NAME_RE.search(company):
                        continue
                    ciks = src.get("ciks") or [""]
                    cik = ciks[0].lstrip("0") or "0"
                    file_date = src.get("file_date", "")
                    locations = src.get("biz_locations") or [""]
                    items.append({
                        "id": f"edgar:{adsh}",
                        "source": "edgar",
                        "signal": "funded",
                        "company": company,
                        "title": f"Form D filed {file_date}",
                        "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                               f"{adsh.replace('-', '')}/{adsh}-index.htm",
                        "location": locations[0],
                        "posted_at": file_date,
                    })
                if len(hits) < 10:
                    break
    except (requests.exceptions.RequestException, ValueError) as e:
        return {"error": f"EDGAR poll failed: {e}"}
    return {"items": items}


# Titles worth flagging on a watched board: senior product/eng leadership.
_LEADERSHIP_RE = re.compile(
    r"(?:\b(?:vp|vice president|head|director|chief)\b.{0,40}"
    r"\b(?:product|engineering|technolog)|\b(?:cpo|cto|cpto)\b)",
    re.IGNORECASE,
)


def _is_leadership(title: str) -> bool:
    return bool(_LEADERSHIP_RE.search(title or ""))


def _lever_iso(created_ms) -> Optional[str]:
    try:
        return datetime.fromtimestamp(int(created_ms) / 1000, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return None


def _ats_board_jobs(entry: dict) -> list:
    """Fetch one watched board and normalize its leadership openings.
    Raises requests exceptions — the caller degrades per board."""
    ats, slug, company = entry["ats"], entry["slug"], entry["company"]
    out = []
    if ats == "greenhouse":
        resp = requests.get(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=_TIMEOUT_S
        )
        resp.raise_for_status()
        for job in resp.json().get("jobs", []):
            if not _is_leadership(job.get("title")):
                continue
            out.append({
                "id": f"greenhouse:{slug}:{job.get('id')}",
                "title": job.get("title", ""),
                "url": job.get("absolute_url", ""),
                "location": (job.get("location") or {}).get("name", ""),
                "posted_at": job.get("first_published"),
            })
    elif ats == "lever":
        resp = requests.get(
            f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"},
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        for job in resp.json():
            if not _is_leadership(job.get("text")):
                continue
            out.append({
                "id": f"lever:{slug}:{job.get('id')}",
                "title": job.get("text", ""),
                "url": job.get("hostedUrl", ""),
                "location": (job.get("categories") or {}).get("location", ""),
                "posted_at": _lever_iso(job.get("createdAt")),
            })
    elif ats == "ashby":
        resp = requests.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=_TIMEOUT_S
        )
        resp.raise_for_status()
        for job in resp.json().get("jobs", []):
            if not job.get("isListed", True) or not _is_leadership(job.get("title")):
                continue
            out.append({
                "id": f"ashby:{slug}:{job.get('id')}",
                "title": job.get("title", ""),
                "url": job.get("jobUrl", ""),
                "location": job.get("location", ""),
                "posted_at": job.get("publishedAt"),
            })
    for job in out:
        job.update({"source": "ats", "signal": "hiring", "company": company})
    return out


def poll_ats(watchlist: list) -> dict:
    """Leadership openings across every watched board. One board failing is
    reported in `errors` but never blocks the others."""
    items, errors = [], []
    for entry in watchlist:
        try:
            items.extend(_ats_board_jobs(entry))
        except (requests.exceptions.RequestException, ValueError) as e:
            errors.append(f"{entry['company']} ({entry['ats']}/{entry['slug']}): {e}")
    return {"items": items, "errors": errors}


# Phrases that make an HN "Who is hiring" post worth scoring. Deliberately
# leadership-shaped — plain "hiring engineers" posts are out of scope.
_HN_PHRASES = (
    "head of product", "vp of product", "vp product", "director of product",
    "product lead", "founding pm", "founding product",
    "head of engineering", "vp of engineering", "vp engineering",
    "founding engineer", "fractional", "cto", "cpo",
)


def _hn_clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(text or ""))).strip()


def poll_hn() -> dict:
    """Leadership-flavored posts from the current HN 'Who is hiring' thread."""
    try:
        resp = requests.get(
            HN_SEARCH_URL,
            params={"tags": "story,author_whoishiring", "hitsPerPage": 5},
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        story = next(
            (h for h in resp.json().get("hits", [])
             if "who is hiring" in (h.get("title") or "").lower()),
            None,
        )
        if story is None:
            return {"items": []}
        story_id = story["objectID"]

        items = []
        for page in range(_HN_MAX_PAGES):
            resp = requests.get(
                HN_SEARCH_URL,
                params={"tags": f"comment,story_{story_id}", "hitsPerPage": 200, "page": page},
                timeout=_TIMEOUT_S,
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
            for hit in hits:
                # Top-level comments are the job posts; replies are chatter.
                if str(hit.get("parent_id")) != str(story_id):
                    continue
                text = _hn_clean(hit.get("comment_text"))
                lowered = text.lower()
                if not any(p in lowered for p in _HN_PHRASES):
                    continue
                # HN convention: "Company | Role | Location | ..." on line one.
                first_segment = text.split("|", 1)[0].strip()
                company = first_segment[:60] or "(unknown)"
                items.append({
                    "id": f"hn:{hit.get('objectID')}",
                    "source": "hn",
                    "signal": "hiring",
                    "company": company,
                    "title": text[:100],
                    "url": f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                    "location": "",
                    "posted_at": hit.get("created_at"),
                    "snippet": text[:_SNIPPET_CHARS],
                })
            if len(hits) < 200:
                break
    except (requests.exceptions.RequestException, ValueError, KeyError) as e:
        return {"error": f"HN poll failed: {e}"}
    return {"items": items}


# ---- scoring ---------------------------------------------------------------

def _compact_for_scoring(items: list) -> list:
    """The subset of fields the model needs — bounds the prompt."""
    return [
        {"id": i["id"], "signal": i["signal"], "company": i["company"],
         "title": i.get("title") or "", "location": i.get("location") or "",
         "snippet": (i.get("snippet") or "")[:_SNIPPET_CHARS]}
        for i in items
    ]


def _parse_scores(text: str, valid_ids: set) -> dict:
    """Parse 'id|score|angle' lines defensively: unknown ids, bad scores, and
    stray prose are dropped rather than crashing the digest. Items the model
    skipped simply stay unscored."""
    scores = {}
    for line in (text or "").splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) < 2 or parts[0].strip() not in valid_ids:
            continue
        try:
            score = max(1, min(10, int(parts[1].strip())))
        except ValueError:
            continue
        angle = parts[2].strip() if len(parts) == 3 else ""
        scores[parts[0].strip()] = (score, angle)
    return scores


def score_items(items: list, logger: Optional[logging.Logger] = None) -> dict:
    to_score = items[:MAX_SCORE_ITEMS]
    if not to_score:
        return {}
    raw = complete_text(
        system_prompt=SCORING_SYSTEM_PROMPT,
        user_prompt=f"leads: {json.dumps(_compact_for_scoring(to_score))}",
    )
    if logger:
        logger.info(f"scoring output ->\n{raw}")
    return _parse_scores(raw, {i["id"] for i in to_score})


# ---- digest ----------------------------------------------------------------

_STYLE = """
  <style>
    body { margin: 0; padding: 0; background: #f4f4f5; }
    .wrap { max-width: 640px; margin: 0 auto; padding: 24px 20px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; color: #1f2328; }
    .card { background: #ffffff; border-radius: 12px; padding: 28px 32px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
    h1 { font-size: 22px; margin: 0 0 4px; }
    .date { color: #6b7280; font-size: 14px; margin: 0 0 16px; }
    hr { border: none; border-top: 1px solid #e5e7eb; margin: 0 0 20px; }
    .section { margin-bottom: 22px; }
    .section:last-child { margin-bottom: 0; }
    .section-title { font-size: 15px; font-weight: 600; margin: 0 0 8px; }
    .section-body { font-size: 14px; line-height: 1.55; color: #374151; }
    ul { margin: 0; padding-left: 20px; }
    li { margin-bottom: 10px; }
    .score { display: inline-block; background: #eef2ff; color: #3730a3; border-radius: 8px; padding: 0 6px; font-size: 12px; font-weight: 600; }
    .angle { color: #6b7280; font-style: italic; }
    .meta { color: #9ca3af; font-size: 12px; }
    a { color: #2563eb; text-decoration: none; }
  </style>
"""

_SECTIONS = (
    ("funded", "\U0001F195", "Just Funded"),
    ("stalled_search", "⏳", "Stalled Searches"),
    ("hiring", "\U0001F44B", "Hiring Signals"),
)


def _safe_url(url: str) -> str:
    """Return url only if it's an http(s) link, else "". Guards against
    javascript:/data: (or other) schemes in externally-sourced URLs —
    html.escape() alone does not neutralize a dangerous scheme."""
    try:
        return url if urlparse(url).scheme in ("http", "https") else ""
    except (ValueError, AttributeError):
        return ""


def _item_html(item: dict) -> str:
    company = html.escape(item["company"])
    url = _safe_url(item.get("url") or "")
    name_html = f'<a href="{html.escape(url)}">{company}</a>' if url else company
    title = html.escape(item.get("title") or "")
    score = item.get("score")
    score_html = f' <span class="score">{int(score)}/10</span>' if score else ""
    angle = html.escape(item.get("angle") or "")
    angle_html = f'<br><span class="angle">{angle}</span>' if angle else ""
    location = html.escape(item.get("location") or "")
    meta_html = f' <span class="meta">{location}</span>' if location else ""
    return (f"<li><strong>{name_html}</strong>{score_html} — {title}"
            f"{meta_html}{angle_html}</li>")


def render_digest_html(items: list) -> str:
    date_line = datetime.now().strftime("%A, %B %-d")
    sections = []
    for signal, icon, label in _SECTIONS:
        # Highest-scored first; unscored last.
        matching = sorted(
            (i for i in items if i["signal"] == signal),
            key=lambda i: i.get("score") or 0,
            reverse=True,
        )
        if not matching:
            continue
        lis = "".join(_item_html(i) for i in matching)
        sections.append(
            f'<div class="section"><p class="section-title">{icon} '
            f"{html.escape(label)} ({len(matching)})</p>"
            f'<div class="section-body"><ul>{lis}</ul></div></div>'
        )
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8">{_STYLE}</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Opportunity Digest</h1>
      <p class="date">{html.escape(date_line)}</p>
      <hr>
      {"".join(sections)}
    </div>
  </div>
</body>
</html>"""


# Exposed so the chat agent can trigger the real deterministic pipeline on
# request instead of freehand-composing a digest-like email itself.
SEND_DIGEST_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "send_opportunity_digest",
        "description": (
            "Run Craig's opportunity scout right now — poll SEC Form D filings, "
            "watched job boards, and HN Who-is-hiring; score any NEW leads; and "
            "email the Opportunity Digest (nothing is sent if there's nothing "
            "new). Use whenever Craig asks to check for opportunities or send "
            "the digest — do NOT compose that email yourself with send_email."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def build_and_send_digest(logger: Optional[logging.Logger] = None) -> dict:
    """Poll all sources, dedupe into the store, score new items, and email the
    digest. Shared by the scheduled task (main, below) and the chat agent's
    send_opportunity_digest tool so both paths produce identical output."""
    log = logger or logging.getLogger("opportunity_digest")
    try:
        # Capture "today" before polling so the watermark written after a
        # successful run never skips filings made during this run.
        run_day = date.today().isoformat()
        edgar_start = _read_edgar_watermark() or (
            date.today() - timedelta(days=3)
        ).isoformat()

        edgar_result = poll_edgar(edgar_start, run_day, _states())
        log.info(f"poll_edgar({edgar_start}..{run_day}) -> "
                 f"{len(edgar_result.get('items', []))} items, "
                 f"error={edgar_result.get('error')}")

        watchlist = opportunities.get_watchlist()
        ats_result = poll_ats(watchlist)
        for err in ats_result.get("errors", []):
            log.warning(f"ATS board poll failed: {err}")
        log.info(f"poll_ats({len(watchlist)} boards) -> "
                 f"{len(ats_result.get('items', []))} leadership openings")

        hn_result = poll_hn()
        log.info(f"poll_hn -> {len(hn_result.get('items', []))} items, "
                 f"error={hn_result.get('error')}")

        # Each source degrades to [] on error — one dead feed never kills the
        # digest, matching the weekly-learnings posture.
        candidates = (
            edgar_result.get("items", [])
            + ats_result.get("items", [])
            + hn_result.get("items", [])
        )
        inserted = opportunities.insert_new_items(candidates)
        log.info(f"{len(inserted)} new of {len(candidates)} polled")

        open_postings = {i["id"]: i.get("posted_at")
                         for i in ats_result.get("items", [])}
        flipped = opportunities.flip_stalled(open_postings, _stalled_days())
        if flipped:
            log.info(f"{len(flipped)} openings crossed the "
                     f"{_stalled_days()}-day stalled threshold")

        to_report = opportunities.pending_new_items()
        if not to_report:
            log.info("Nothing new — no digest sent")
            if not edgar_result.get("error"):
                _write_edgar_watermark(run_day)
            return {"sent": False, "new_items": 0}

        unscored = [i for i in to_report if i.get("score") is None]
        scores = score_items(unscored, log)
        opportunities.record_scores(scores)
        for item in to_report:
            if item["id"] in scores:
                item["score"], item["angle"] = scores[item["id"]]

        body_html = render_digest_html(to_report)
        result = send_email(
            subject=f"Opportunity Digest - {today_str()}",
            body=body_html,
            html=True,
        )
        log.info(f"send_email -> {result}")
        if "error" in result:
            return result

        opportunities.mark_digested([i["id"] for i in to_report])
        if not edgar_result.get("error"):
            _write_edgar_watermark(run_day)

        high = [i for i in to_report if (i.get("score") or 0) >= _score_threshold()]
        if high:
            top = max(high, key=lambda i: i["score"])
            notify(
                message=f"{len(high)} strong lead(s) — top: {top['company']} "
                        f"({top['score']}/10). Digest is in your inbox.",
                title="Wren: opportunity scout",
            )

        log.info(f"Digest sent: {len(to_report)} items, {len(high)} high-scoring")
        return {"sent": True, "new_items": len(to_report), "high_scoring": len(high)}
    except Exception as e:
        log.exception(f"Opportunity digest run failed: {e}")
        return {"error": str(e)}


def main() -> int:
    logger = setup_logger("opportunity_digest")
    result = build_and_send_digest(logger=logger)
    if "error" in result:
        # Push only from the scheduled run — the chat tool shares
        # build_and_send_digest() but the user is present there to see the
        # error, so the alert lives here in main().
        notify_failure("opportunity_digest", result["error"], logger)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
