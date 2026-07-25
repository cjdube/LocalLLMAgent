"""Connect yesterday's activity to Craig's own accumulated knowledge and push a
few high-confidence "these line up" nudges — Wren's first proactive-synthesis
task. Non-interactive, run by launchd every morning after the daily learnings
tasks have populated the day's signals.

The design follows the small-local-model constraint (see CLAUDE.md): Python owns
the structure. It reads yesterday's browsing + YouTube Likes (the "signals") and
Craig's wiki pages + watched/interesting companies (the "anchors"), and does the
matching itself via token overlap — the model never rummages through everything
looking for connections (a small model manufactures those). The model's only job
is a bounded pass over a short, pre-matched candidate list: keep the genuine
connections, drop the coincidental ones, write one line each. No overlap, or no
genuine connection, means nothing is pushed — silence is the common case.

The live output is an optional push; a dated copy is archived to SYNTHESIS_DIR so
suggestions survive it. That directory is deliberately NOT the vault's raw/
(LEARNINGS_DIR): raw/ is ObsidianWikiAgent's ingest queue, and these files are
questions addressed to Craig, not sources. Ingesting them produced dated orphan
wiki pages that restated "is this worth adding?" as "the current focus involves
…" — a fabrication by restatement. Nothing consequential is done either way, so
there is no confirmation gate.

Usage:
    python -m tasks.daily_synthesis
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.loop import complete_text, resolve_backend, warm_model
from agent import prefs
from agent.tools.chrome_history import fetch_chrome_history
from agent.tools.notify import notify
from agent.tools.opportunities import get_watchlist, list_opportunities
from agent.tools.wiki import list_wiki_pages
from agent.tools.youtube import fetch_liked_videos
from tasks._common import notify_failure, setup_logger
from tasks._learnings_common import (
    MAX_PAGES_PER_SITE,
    compact_sites,
    persist_or_email,
    prior_day,
)

# Where the nudge archive lands: a vault folder ObsidianWikiAgent does not walk
# (it ingests raw/ only), so Obsidian can still browse the history without the
# ingest agent treating a question as a source. See the module docstring.
DEFAULT_SYNTHESIS_DIR = str(Path.home() / "Documents" / "llm-wiki-learnings" / "nudges")

# How many pre-matched pairs the model judges, and how many nudges it may emit.
# Kept small on purpose: the candidate list is context the small model reasons
# over, and more than a couple of nudges a day is noise, not signal.
MAX_CANDIDATES = 8
MAX_NUDGES = 3

# Minimum token length for overlap matching. Short tokens ("api", "the", "app")
# are too generic to anchor a real connection; the model filters the rest.
_MIN_TOKEN_LEN = 4

# Long-but-generic words that would otherwise create spurious matches between
# any tech page and any tech note. Deliberately short — the model is the real
# filter; this only trims the most obvious noise before it gets there.
_STOPWORDS = {
    "http", "https", "html", "www", "com", "docs", "documentation", "guide",
    "home", "page", "index", "blog", "news", "your", "with", "from", "this",
    "that", "what", "using", "about", "into", "over", "more", "have", "will",
}

SYNTHESIS_SYSTEM_PROMPT = f"""You are {prefs.user_name()}'s personal assistant. \
Below is a shortlist of CANDIDATE connections. Each pairs something {prefs.user_name()} \
did yesterday (a site he browsed, or a video he Liked) with something already in his \
world (a page in his notes wiki, or a company he tracks). A dumb keyword match put \
them together — your job is to decide which are GENUINE, useful connections worth \
telling him about, and which are coincidental.

Keep ONLY the genuine, non-obvious, actionable ones. For each, write ONE short line \
that names both sides and why they connect — phrased as a nudge he can act on, e.g. \
"You dug into DuckDB yesterday — it fits your 'local-analytics' note; want a summary?".

Output rules:
- One nudge per line, each starting with "- ".
- At most {MAX_NUDGES} lines. Fewer is better. Quality over quantity.
- If NONE of the candidates is a genuine, useful connection, output exactly: NONE
- Output only the lines (or NONE) — no preamble, no headings, no explanation."""


def _synthesis_dir() -> Path:
    """Read at call time, like learnings_file._learnings_dir()."""
    return Path(os.getenv("SYNTHESIS_DIR", DEFAULT_SYNTHESIS_DIR)).expanduser()


def _tokenize(text: str) -> set:
    """Lowercase alphanumeric tokens of length >= _MIN_TOKEN_LEN, minus the
    generic stopwords. The unit of overlap between a signal and an anchor."""
    tokens = re.split(r"[^a-z0-9]+", (text or "").lower())
    return {t for t in tokens if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS}


def gather_signals(start, end, logger) -> list:
    """Yesterday's activity as {text, tokens} rows. Each source is guarded: a
    dead source contributes nothing rather than killing the run (CLAUDE.md:
    degrade, don't crash)."""
    signals = []

    try:
        chrome = fetch_chrome_history(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
                                      pages_per_domain=MAX_PAGES_PER_SITE)
        for site in compact_sites(chrome.get("sites", [])):
            paths = " ".join(site.get("pages") or [])
            text = f"{site.get('title') or ''} ({site.get('domain') or ''}) {paths}".strip()
            signals.append({"kind": "browsed", "text": text,
                            "tokens": _tokenize(f"{text} {site.get('domain') or ''}")})
    except Exception as e:
        logger.warning(f"chrome signals unavailable: {e}")

    try:
        yt = fetch_liked_videos(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        for video in yt.get("videos", []):
            title = video.get("title") or ""
            signals.append({"kind": "watched", "text": f"{title} — {video.get('channel') or ''}".strip(" —"),
                            "tokens": _tokenize(title)})
    except Exception as e:
        logger.warning(f"youtube signals unavailable: {e}")

    return signals


def gather_anchors(logger) -> list:
    """Craig's existing world as {label, tokens} rows: wiki page names and the
    companies he watches or has marked interesting. Same per-source guard."""
    anchors = []

    try:
        for page in list_wiki_pages().get("pages", []):
            name = page[:-3] if page.endswith(".md") else page
            anchors.append({"kind": "wiki page", "label": name,
                            "tokens": _tokenize(name.replace("-", " "))})
    except Exception as e:
        logger.warning(f"wiki anchors unavailable: {e}")

    companies = {}
    try:
        for w in get_watchlist():
            if w.get("company"):
                companies[w["company"].lower()] = w["company"]
    except Exception as e:
        logger.warning(f"watchlist anchors unavailable: {e}")
    try:
        for item in list_opportunities(status="interested").get("opportunities", []):
            if item.get("company"):
                companies.setdefault(item["company"].lower(), item["company"])
    except Exception as e:
        logger.warning(f"opportunity anchors unavailable: {e}")
    for company in companies.values():
        anchors.append({"kind": "company you track", "label": company,
                        "tokens": _tokenize(company)})

    return anchors


def candidate_pairs(signals: list, anchors: list) -> list:
    """Pair each signal with each anchor that shares a token, scored by overlap
    size, best first, capped at MAX_CANDIDATES. Pure — the whole matching step."""
    pairs = []
    for sig in signals:
        for anc in anchors:
            overlap = sig["tokens"] & anc["tokens"]
            if overlap:
                pairs.append({"score": len(overlap), "signal": sig, "anchor": anc,
                              "overlap": sorted(overlap)})
    pairs.sort(key=lambda p: p["score"], reverse=True)
    return pairs[:MAX_CANDIDATES]


def render_candidates(pairs: list) -> str:
    """The candidate shortlist as the model's user prompt."""
    lines = []
    for i, p in enumerate(pairs, 1):
        sig, anc = p["signal"], p["anchor"]
        lines.append(
            f"{i}. yesterday he {sig['kind']}: \"{sig['text']}\"\n"
            f"   existing {anc['kind']}: \"{anc['label']}\"\n"
            f"   shared terms: {', '.join(p['overlap'])}"
        )
    return "\n".join(lines)


def parse_nudges(text: str) -> list:
    """Pull the "- " bullet lines the model emitted, capped at MAX_NUDGES. A
    lone NONE (or no bullets) yields nothing — the silence path."""
    nudges = [line.strip()[2:].strip()
              for line in (text or "").splitlines()
              if line.strip().startswith("- ") and line.strip()[2:].strip()]
    return nudges[:MAX_NUDGES]


def main() -> int:
    logger = setup_logger("daily_synthesis")
    logger.info("Starting daily synthesis run")

    try:
        start, end, day = prior_day()
        logger.info(f"Day: {day}")

        signals = gather_signals(start, end, logger)
        anchors = gather_anchors(logger)
        logger.info(f"{len(signals)} signal(s), {len(anchors)} anchor(s)")

        candidates = candidate_pairs(signals, anchors)
        logger.info(f"{len(candidates)} candidate connection(s) after overlap match")
        if not candidates:
            # No token overlap between yesterday and Craig's world — the common
            # case. Log the run-complete boundary (the dashboard reads status
            # from the log) and stop before warming the model.
            logger.info("No overlaps; nothing to synthesize")
            logger.info("Daily synthesis run complete")
            return 0

        backend = resolve_backend("daily_synthesis")
        warm_model(logger=logger, backend=backend)
        raw = complete_text(system_prompt=SYNTHESIS_SYSTEM_PROMPT,
                            user_prompt=render_candidates(candidates), logger=logger,
                            backend=backend)
        logger.info(f"Model output:\n{raw}")

        nudges = parse_nudges(raw)
        if not nudges:
            logger.info("No genuine connections; nothing to push")
            logger.info("Daily synthesis run complete")
            return 0

        message = "\n".join(f"• {n}" for n in nudges)
        # One-shot alert (nothing retries it), so fall back to email if the push
        # fails — same "don't lose it" choice as notify_failure. The push is the
        # live channel; the vault write below is the durable archive.
        result = notify(message=message, title="Wren: connections worth a look",
                        priority="default", email_fallback=True)
        if result.get("error"):
            logger.warning(f"synthesis push did not send: {result['error']}")
        logger.info(f"Pushed {len(nudges)} nudge(s)")

        # Persist a reviewable copy: one file per day in SYNTHESIS_DIR, outside the
        # vault's ingest queue (see the module docstring). The push scrolls off the
        # phone; this is the record Craig scrolls back through later. Each nudge
        # already names both sides, so the bullet list is self-contained.
        body = (f"## Synthesis Suggestions: {day:%B %-d, %Y}\n\n"
                + "\n".join(f"- {n}" for n in nudges) + "\n")
        persist_or_email(
            body, "Daily-Synthesis", day,
            subject=f"Synthesis Suggestions (needs manual paste) - {day:%Y-%m-%d}",
            task_name="daily_synthesis", logger=logger, directory=_synthesis_dir(),
        )
        logger.info("Daily synthesis run complete")
        return 0
    except Exception as e:
        logger.exception(f"Daily synthesis run failed: {e}")
        notify_failure("daily_synthesis", e, logger)
        return 1


if __name__ == "__main__":
    sys.exit(main())
