"""Connect yesterday's activity to the user's own accumulated knowledge and push a
few high-confidence "these line up" nudges — Wren's first proactive-synthesis
task. Non-interactive, run by launchd every morning after the daily learnings
tasks have populated the day's signals.

The design follows the small-local-model constraint (see CLAUDE.md): Python owns
the structure. It reads yesterday's browsing + YouTube Likes + AI-agent chats (the
"signals") and the user's wiki pages + watched/interesting companies (the "anchors"),
and does the matching itself via token overlap — the model never rummages through
everything looking for connections (a small model manufactures those). The model's
only job is a bounded pass over a short, pre-matched candidate list: keep the
genuine connections, drop the coincidental ones, write one line each. No overlap,
or no genuine connection, means nothing is pushed — silence is the common case.

Two kinds of candidate go into that list:

- CONNECTION — a signal against an anchor. What the task shipped with.
- ECHO — a signal against a signal *from a different channel*. The same theme
  reaching him twice independently in one day is itself a signal about what he's
  actually chewing on, and no single-source pass can see it.

The live output is an optional push; a dated copy is archived to SYNTHESIS_DIR so
suggestions survive it. That directory is deliberately NOT the vault's raw/
(LEARNINGS_DIR): raw/ is ObsidianWikiAgent's ingest queue, and these files are
questions addressed to the user, not sources. Ingesting them produced dated orphan
wiki pages that restated "is this worth adding?" as "the current focus involves
…" — a fabrication by restatement. Nothing consequential is done either way, so
there is no confirmation gate.

Usage:
    python -m tasks.daily_synthesis
"""

import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.loop import complete_text, resolve_backend, warm_model
from agent import prefs
from agent.tools.chrome_history import fetch_chrome_history
from agent.tools.learnings_file import read_entry
from agent.tools.notify import notify
from agent.tools.opportunities import get_watchlist, list_opportunities
from agent.tools.projects import load_registry
from agent.tools.wiki import list_project_pages, page_summaries
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

# How many pre-matched pairs of each kind the model judges, and how many nudges it
# may emit. Kept small on purpose: the candidate list is context the small model
# reasons over, and more than a couple of nudges a day is noise, not signal. The
# two caps are separate so the echoes (usually few) can't be crowded out by a
# browsing day that generates dozens of anchor matches, or vice versa.
MAX_ANCHOR_CANDIDATES = 5
MAX_CROSS_CANDIDATES = 3
MAX_NUDGES = 3

# A wiki page's summary as shown to the model. One line in the vault, so this rarely
# binds; it's here so one long summary can't dominate the shortlist's size.
MAX_ANCHOR_SUMMARY_CHARS = 200

# Hard ceiling on a project anchor's token set. A project has far more text
# available than a wiki page — a README, a CLAUDE.md, a docs tree — and feeding
# that in raw would rebuild the bug _ai_chat_signals documents below: a token set
# that large overlaps everything and outranks every real pair. tasks/project_scan.py
# already distils each project to a summary and ~15 topics for this reason; this is
# the belt-and-braces bound, so a project anchor stays the same size class as a wiki
# page's (~20-25 tokens) no matter what the model returns. Tokens are taken in
# priority order — name, then summary, then topics — so the truncation drops the
# tail of the topic list rather than the project's own name.
MAX_PROJECT_ANCHOR_TOKENS = 40

# Bullets taken from the AI-chat log. Bounds the prompt on a heavy chat day; the
# file's bullets are ordered by session, so this keeps the earliest ones.
MAX_AI_CHAT_BULLETS = 20

# Minimum token length for overlap matching. Short tokens ("api", "the", "app")
# are too generic to anchor a real connection; the model filters the rest.
_MIN_TOKEN_LEN = 4

# Above this many tokens, a description is broad enough that sharing exactly one
# word with another broad description is coincidence rather than signal — so a
# pair of broad sides needs _MIN_BROAD_OVERLAP shared tokens to qualify. The rule
# keys off the *smaller* side: a one-token anchor like the page `gemma-4` has only
# one token to offer, and that single match is the strongest it can be.
_BROAD_SET_SIZE = 8
_MIN_BROAD_OVERLAP = 2

# When two signals share nearly all of the smaller one's tokens AND share several of
# them, they are the same artifact seen twice, not two things about one theme — a
# liked video whose link also sits in Chrome history, or a newsletter that ships the
# same post as a video. Both thresholds are needed: the ratio alone would also drop
# a legitimately thin echo (one page and one video sharing only "duckdb"), which is
# a real echo, just a small one.
_DUP_OVERLAP_RATIO = 0.75
_DUP_MIN_OVERLAP = 4

# An echo claims "the same theme reached him twice", and one shared word is not a
# theme — on real data a GitHub repo page and the chat bullet "Committed all changes
# to `main`" became an echo on the token "main". Anchor pairs keep the single-token
# rule (a wiki page named `gemma-4` has only one token to match), but a two-signal
# pair always has richer text on both sides, so it can afford to be asked for more.
_MIN_ECHO_OVERLAP = 2

# Share of the anchor corpus a token can appear in before it stops discriminating.
# Anchoring on page *summaries* rather than names is what makes this necessary: prose
# is full of words that are everywhere in the vault, and the first run of it matched
# on {agent, design}, {backend, local}, {dashboard, wren} and {agent, through} — four
# of five candidates were vocabulary coincidences rather than topical connections. A
# fixed stopword list can't reach this (it would have to contain half of the user's
# domain), so the set is computed from the corpus each run and tracks what his wiki is
# actually about. Echoes are exempt: there the coincidence is *temporal* — the same
# word arriving through two channels in one day means something even if the word is
# common — and the anchor corpus isn't involved in the pair at all.
_GENERIC_TOKEN_SHARE = 0.05

# ...but never call a token filler on the evidence of fewer than this many pages. The
# share alone would make any token shared by two pages generic in a ten-page vault,
# which is a statistic about the sample size, not about the token.
_GENERIC_TOKEN_FLOOR = 5

# Long-but-generic words that would otherwise create spurious matches between
# any tech page and any tech note. Deliberately short — the model is the real
# filter; this only trims the most obvious noise before it gets there.
_STOPWORDS = {
    "http", "https", "html", "www", "com", "docs", "documentation", "guide",
    "home", "page", "index", "blog", "news", "your", "with", "from", "this",
    "that", "what", "using", "about", "into", "over", "more", "have", "will",
}

SYNTHESIS_SYSTEM_PROMPT = f"""You are {prefs.user_name()}'s personal assistant. \
Below is a shortlist of CANDIDATES, put together by a dumb keyword match. Your job \
is to decide which are GENUINE, useful connections worth telling him about, and \
which are coincidental. There are two kinds:

- CONNECTION — something {prefs.user_name()} did yesterday (browsed a site, Liked a \
video, worked through something with an AI agent) lines up with something already in \
his world (a page in his notes wiki, or a company he tracks).
- ECHO — the same theme reached him yesterday through two independent channels. The \
repetition is the point: it says what he is actually chewing on. Say so plainly, e.g. \
"Stripe's PM archetypes came at you twice yesterday — reading and video; worth a note?".

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


# The AI-chat log's section headers ("**Learned**") and the two empty-section
# markers the learnings templates use.
_SECTION_RE = re.compile(r"^\*\*(\w+)\*\*$")

# Wiki pages whose name ends in a date are ObsidianWikiAgent's write-ups of the
# daily/weekly logs — `daily-chrome-2026-07-24`, `ai-chat-learnings-2026-07-01`,
# `strategic-weekly-review-...`. They are records of the user's activity, not concept
# knowledge, so matching yesterday's activity against them is circular: on real data
# yesterday's browsing matched the page written from yesterday's browsing. 49 of the
# vault's 203 pages, so excluding them also frees up a quarter of the anchor set.
_DATED_PAGE_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")


def _is_none_marker(bullet: str) -> bool:
    """True for a bullet that is a template placeholder rather than a topic: "- None"
    (the AI-chat log) or "- **None:** ..." (the daily logs)."""
    text = bullet[2:].strip()
    return not text or text.lower() == "none" or "**None:**" in text


def _tokenize(text: str) -> set:
    """Lowercase alphanumeric tokens of length >= _MIN_TOKEN_LEN, minus the
    generic stopwords. The unit of overlap between a signal and an anchor."""
    tokens = re.split(r"[^a-z0-9]+", (text or "").lower())
    return {t for t in tokens if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS}


def _match(a_tokens: set, b_tokens: set) -> dict | None:
    """Shared tokens plus a size-normalized score, or None if the pair is too weak
    to be a candidate.

    The score is the overlap as a fraction of the *smaller* token set, not the raw
    count: the sets are not the same size (a Chrome signal carries six page paths,
    a wiki page name carries two words), and ranking by raw count just promotes
    whichever side is wordiest. `_BROAD_SET_SIZE` handles the other end — see its
    comment."""
    overlap = a_tokens & b_tokens
    if not overlap:
        return None
    smaller = min(len(a_tokens), len(b_tokens))
    if len(overlap) < _MIN_BROAD_OVERLAP and smaller > _BROAD_SET_SIZE:
        return None
    return {"score": len(overlap) / smaller, "overlap": sorted(overlap)}


def _ai_chat_signals(day, logger) -> list:
    """Yesterday's AI-agent chats, read from the daily log tasks/ai_chat_learnings.py
    writes at 4:30 AM (this task runs at 5:45).

    Deliberately the distilled file rather than the raw transcripts: a session's
    text is capped at 12k chars, and a token set that large overlaps everything, so
    it would match every anchor and rank above every real pair. The file's bullets
    are one topic each — title-sized, like the other channels' signals. No file
    (nothing chatted, or the 4:30 task hasn't run) means no channel, not a failure.

    Only the "Learned" bullets. The file's other section is "Accomplished", which is
    process ("Committed all changes to `main`") — on real data those matched wiki
    pages on branch and repo names and nothing else. What he learned is the part that
    can echo something else in his day."""
    result = read_entry("AI-Chat-Learnings", day)
    if "error" in result:
        logger.info(f"no AI-chat signals: {result['error']}")
        return []

    signals, in_learned = [], False
    for line in result["content"].splitlines():
        stripped = line.strip()
        section = _SECTION_RE.match(stripped)
        if section:
            in_learned = section.group(1) == "Learned"
            continue
        # "None" is the learnings template's empty-section marker, not a topic.
        if not in_learned or not stripped.startswith("- ") or _is_none_marker(stripped):
            continue
        text = stripped[2:].strip()
        signals.append({"channel": "ai-chat", "kind": "discussed with an AI agent",
                        "text": text, "tokens": _tokenize(text)})
        if len(signals) >= MAX_AI_CHAT_BULLETS:
            break
    return signals


def gather_signals(start, end, day, logger) -> list:
    """Yesterday's activity as {channel, kind, text, tokens} rows. Each source is
    guarded: a dead source contributes nothing rather than killing the run
    (CLAUDE.md: degrade, don't crash). `channel` is what makes an echo detectable —
    see cross_channel_pairs."""
    signals = []

    try:
        chrome = fetch_chrome_history(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
                                      pages_per_domain=MAX_PAGES_PER_SITE)
        for site in compact_sites(chrome.get("sites", [])):
            paths = " ".join(site.get("pages") or [])
            text = f"{site.get('title') or ''} ({site.get('domain') or ''}) {paths}".strip()
            signals.append({"channel": "chrome", "kind": "browsed", "text": text,
                            "tokens": _tokenize(f"{text} {site.get('domain') or ''}")})
    except Exception as e:
        logger.warning(f"chrome signals unavailable: {e}")

    try:
        yt = fetch_liked_videos(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        for video in yt.get("videos", []):
            title = video.get("title") or ""
            signals.append({"channel": "youtube", "kind": "watched",
                            "text": f"{title} — {video.get('channel') or ''}".strip(" —"),
                            "tokens": _tokenize(title)})
    except Exception as e:
        logger.warning(f"youtube signals unavailable: {e}")

    try:
        signals.extend(_ai_chat_signals(day, logger))
    except Exception as e:
        logger.warning(f"AI-chat signals unavailable: {e}")

    return signals


def _summary_head(summary: str) -> str:
    """The summary bounded to MAX_ANCHOR_SUMMARY_CHARS, cut at a word boundary.

    The same bound the *displayed* summary gets, applied before tokenizing too.
    Without it a verbose wiki summary crowds the token budget and pushes the
    project's own topics out: `wren.md` ran to 30 words ("...modeled after the
    high-output, agile characteristics of the wren bird"), which was more than
    the name and all ten topics combined and cost LocalLLMAgent the tokens
    `rest`, `tailscale` and `tool`. A page describing the project *badly* was
    displacing terms taken from the repo itself.

    Cut at a word boundary because a mid-word slice leaves a fragment that is a
    real token: `tailsca` matches nothing, but it still occupies a slot."""
    if len(summary) <= MAX_ANCHOR_SUMMARY_CHARS:
        return summary
    head = summary[:MAX_ANCHOR_SUMMARY_CHARS]
    return head[:head.rfind(" ")] if " " in head else head


def _project_tokens(name: str, summary: str, topics: list) -> set:
    """A project anchor's token set, capped at MAX_PROJECT_ANCHOR_TOKENS and
    filled in priority order — the project's own name first, then what it is,
    then what it's about. Truncation therefore costs the tail of the topic list,
    never the name.

    With `_summary_head` bounding the middle term, a typical project lands near
    30 and the cap is a backstop rather than the thing doing the work."""
    tokens = []
    for text in (name.replace("-", " "), _summary_head(summary), " ".join(topics)):
        for token in sorted(_tokenize(text)):
            if token not in tokens:
                tokens.append(token)
                if len(tokens) >= MAX_PROJECT_ANCHOR_TOKENS:
                    return set(tokens)
    return set(tokens)


def gather_project_anchors(logger) -> tuple[list, set]:
    """The user's own checkouts as anchors, plus the set of wiki page names those
    anchors absorbed. Returns ([], set()) on any failure — a project scan that
    hasn't run costs the merge, not the run.

    This is the one anchor source that knows what he is *building* rather than
    what he has written down, so it reaches connections no wiki page can: an
    article on SSE reconnection has nothing to say to a page about note-taking,
    but plenty to say to the repo that just moved to server-sent events.

    Where a project also has a wiki page (matched on the page's `path:`
    frontmatter — see agent/tools/wiki.py, and note the page for LocalLLMAgent is
    called `wren`, which no slug rule could ever match), the two are merged into
    ONE anchor and the page name is returned so the wiki loop can skip it.
    Without that, the same project stands as two separate anchors, and since
    _one_per_side dedupes by side identity it would happily place both — showing
    the model one story twice, the exact thing that function exists to prevent."""
    try:
        projects = load_registry()
    except Exception as e:
        logger.warning(f"project anchors unavailable: {e}")
        return [], set()
    if not projects:
        return [], set()

    try:
        pages = {p["path"]: p for p in list_project_pages()["projects"] if p.get("path")}
    except Exception as e:
        # The registry is still usable without the vault; only the merge is lost.
        logger.warning(f"wiki project pages unavailable, not merging: {e}")
        pages = {}

    anchors, absorbed = [], set()
    for project in projects:
        name = project.get("name") or ""
        topics = project.get("topics") or []
        page = pages.get(name)
        # The wiki page's summary is preferred: it carries the decisions and
        # rationale the repo's own README leaves out, which is the half of a
        # project the model can actually say something interesting about.
        summary = (page.get("summary") if page else "") or project.get("summary") or ""
        if not summary and not topics:
            # Nothing but a name. Anchoring on that can only match its own
            # spelling — the tautology the docstring above warns about — so skip
            # it. project_scan already logged which projects these are and why.
            continue
        if page:
            absorbed.add(page["name"])
        anchors.append({"kind": "project you're building", "label": name,
                        "summary": summary[:MAX_ANCHOR_SUMMARY_CHARS],
                        "tokens": _project_tokens(name, summary, topics)})

    logger.info(f"{len(anchors)} project anchor(s), {len(absorbed)} merged with a wiki page")
    return anchors, absorbed


def gather_anchors(logger) -> list:
    """The user's existing world as {label, summary, tokens} rows: his own projects,
    wiki pages, and the companies he watches or has marked interesting. Same
    per-source guard.

    A page contributes its `**Summary**:` line as well as its name. Matching on the
    name alone can only find lexical identity — "the thing you looked at is spelled
    like a page you have" — which is tautological by construction and exactly the
    reason the early nudges only ever restated the day back at him. The summary is
    what lets "columnar store" reach a page called `duckdb-analytics`."""
    anchors, absorbed = gather_project_anchors(logger)

    try:
        result = page_summaries()
        if "error" in result:
            logger.warning(f"wiki anchors unavailable: {result['error']}")
        for page in result.get("pages", []):
            name, summary = page["name"], page.get("summary") or ""
            # `absorbed`: this page is already standing as part of a project
            # anchor. See gather_project_anchors.
            if _DATED_PAGE_RE.search(name) or name in absorbed:
                continue
            anchors.append({"kind": "wiki page", "label": name, "summary": summary,
                            "tokens": _tokenize(f"{name.replace('-', ' ')} {summary}")})
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
        # `exact`: half a company name is not the company. "Planet Fitness" matched
        # the publication "UX Planet" on `planet` and, being a two-token anchor,
        # outranked every page in the vault — short anchors win the normalized score.
        anchors.append({"kind": "company you track", "label": company, "exact": True,
                        "tokens": _tokenize(company)})

    return anchors


def _ranked(pairs: list) -> list:
    """Strongest pairs first. Ties on the normalized score break toward the pair
    sharing more terms."""
    pairs.sort(key=lambda p: (p["score"], len(p["overlap"])), reverse=True)
    return pairs


def _one_per_side(pairs: list) -> list:
    """Keep each pair only if neither of its sides has already placed a stronger one,
    `pairs` already ranked. Both directions bite on real data: a Gemini browsing row
    matched `google-gemini` and `google-gemini-api` and took three of five slots, and
    one page (`claude-knowledge-base-design`) matched a tutorial and the video of that
    same tutorial. Either way the shortlist ends up showing the model one story
    several times instead of showing it the day."""
    seen_signals, seen_anchors, kept = set(), set(), []
    for p in pairs:
        signal = p["signal"]["text"]
        # Echoes have two signals and no anchor; key the second side the same way.
        anchor = p["anchor"]["label"] if p["kind"] == "anchor" else p["other"]["text"]
        if signal in seen_signals or anchor in seen_anchors:
            continue
        seen_signals.add(signal)
        seen_anchors.add(anchor)
        kept.append(p)
    return kept


def _same_artifact(match: dict) -> bool:
    """True when a pair is one thing seen twice rather than two things about one
    theme. See _DUP_OVERLAP_RATIO."""
    return (match["score"] >= _DUP_OVERLAP_RATIO
            and len(match["overlap"]) >= _DUP_MIN_OVERLAP)


def generic_tokens(anchors: list) -> set:
    """Tokens common enough across the anchor corpus to carry no signal. See
    _GENERIC_TOKEN_SHARE and _GENERIC_TOKEN_FLOOR."""
    if not anchors:
        return set()
    counts = Counter(token for anchor in anchors for token in anchor["tokens"])
    limit = max(_GENERIC_TOKEN_FLOOR, int(len(anchors) * _GENERIC_TOKEN_SHARE))
    return {token for token, count in counts.items() if count >= limit}


def candidate_pairs(signals: list, anchors: list) -> list:
    """CONNECTION candidates: each signal against each anchor sharing a token that
    still discriminates. Pure — half of the matching step."""
    generic = generic_tokens(anchors)
    pairs = []
    for sig in signals:
        for anc in anchors:
            if anc.get("exact"):
                # A company is matched on its whole name, so no token is generic to
                # it — dropping one would make the name unmatchable.
                m = _match(sig["tokens"], anc["tokens"])
                if not m or set(m["overlap"]) != anc["tokens"]:
                    continue
            else:
                m = _match(sig["tokens"] - generic, anc["tokens"] - generic)
                if not m:
                    continue
            pairs.append({"kind": "anchor", "signal": sig, "anchor": anc, **m})
    return _one_per_side(_ranked(pairs))[:MAX_ANCHOR_CANDIDATES]


def cross_channel_pairs(signals: list) -> list:
    """ECHO candidates: signals from DIFFERENT channels that share tokens — the same
    theme arriving twice independently in one day.

    Same-channel pairs are excluded: two pages on one site, or two bullets from one
    chat, sharing a word is one thing seen once, not an echo.

    So are same-artifact pairs (_same_artifact), and that filter is doing most of the
    work: chrome_history.NOISE_DOMAINS drops youtube.com but not youtu.be or the
    newsletter redirectors, so a Liked video's own link is usually in the day's
    browsing too. On real data all three echo slots went to one video and one article
    matched against themselves before this."""
    pairs = []
    for i, a in enumerate(signals):
        for b in signals[i + 1:]:
            if a["channel"] == b["channel"]:
                continue
            m = _match(a["tokens"], b["tokens"])
            if m and len(m["overlap"]) >= _MIN_ECHO_OVERLAP and not _same_artifact(m):
                pairs.append({"kind": "cross", "signal": a, "other": b, **m})
    return _one_per_side(_ranked(pairs))[:MAX_CROSS_CANDIDATES]


def render_candidates(pairs: list) -> str:
    """The candidate shortlist as the model's user prompt. The CONNECTION/ECHO label
    matches the two kinds the system prompt describes."""
    lines = []
    for i, p in enumerate(pairs, 1):
        sig = p["signal"]
        if p["kind"] == "cross":
            other = p["other"]
            lines.append(
                f"{i}. ECHO — yesterday he {sig['kind']}: \"{sig['text']}\"\n"
                f"   and he {other['kind']}: \"{other['text']}\"\n"
                f"   shared terms: {', '.join(p['overlap'])}"
            )
        else:
            anc = p["anchor"]
            # The summary is what makes a match judgeable: "google-stitch" alone tells
            # the model nothing about whether the connection is real.
            summary = (anc.get("summary") or "")[:MAX_ANCHOR_SUMMARY_CHARS]
            lines.append(
                f"{i}. CONNECTION — yesterday he {sig['kind']}: \"{sig['text']}\"\n"
                f"   existing {anc['kind']}: \"{anc['label']}\""
                + (f" — {summary}" if summary else "") + "\n"
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

        signals = gather_signals(start, end, day, logger)
        anchors = gather_anchors(logger)
        logger.info(f"{len(signals)} signal(s) {dict(Counter(s['channel'] for s in signals))}, "
                    f"{len(anchors)} anchor(s), "
                    f"{len(generic_tokens(anchors))} token(s) too common to discriminate")

        candidates = candidate_pairs(signals, anchors) + cross_channel_pairs(signals)
        logger.info(f"{len(candidates)} candidate(s) after overlap match "
                    f"({sum(1 for c in candidates if c['kind'] == 'cross')} echo)")
        if not candidates:
            # No token overlap between yesterday and the user's world — the common
            # case. Log the run-complete boundary (the dashboard reads status
            # from the log) and stop before warming the model.
            logger.info("No overlaps; nothing to synthesize")
            logger.info("Daily synthesis run complete")
            return 0

        # Log what the model was given, not just what it returned: a thin nudge is
        # either a bad candidate or bad judgment, and the output alone can't say which.
        prompt = render_candidates(candidates)
        logger.info(f"Candidates:\n{prompt}")

        backend = resolve_backend("daily_synthesis")
        warm_model(logger=logger, backend=backend)
        raw = complete_text(system_prompt=SYNTHESIS_SYSTEM_PROMPT,
                            user_prompt=prompt, logger=logger, backend=backend)
        logger.info(f"Model output:\n{raw}")

        nudges = parse_nudges(raw)
        if not nudges:
            # Three different things end up here, and only one of them is healthy:
            # the model judged nothing genuine (it says NONE), the model returned
            # nothing at all, or it returned prose that held no "- " bullets. They
            # used to share one INFO line, which made a broken run indistinguishable
            # from a quiet one — and silence is this task's common case, so nobody
            # would ever look. Per CLAUDE.md, a task that silently produces LESS is
            # worse than one that fails, because only the failure pushes an alert.
            # These WARNINGs reach the 8am log_inspector; the INFO does not.
            if not (raw or "").strip():
                logger.warning(
                    f"model returned EMPTY content for {len(candidates)} candidate(s) "
                    "— thinking is ON for this call, so the budget may have gone to "
                    "scratchpad; nothing pushed"
                )
            elif "NONE" not in raw:
                logger.warning(
                    f"model returned {len(raw)} chars but no parsable '- ' bullets "
                    f"from {len(candidates)} candidate(s); nothing pushed"
                )
            else:
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
        # phone; this is the record the user scrolls back through later. Each nudge
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
