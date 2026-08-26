"""Scheduled-task template cases for the model bake-off.

These go through agent.loop.complete_text() — the one-shot, tool-free path the
launchd tasks use. Each case carries its task's REAL system prompt and its
task's REAL parser, so "did it pass" here means the same thing it means at
5:45am.

Why this path is in scope at all: thinking tokens are drawn from the same
num_predict budget as the answer, so a model that reasons too long returns
EMPTY content rather than a short answer. That has cost this repo three
incidents (docs/model-constraints.md) and a tool-calling benchmark cannot see
it. A reasoning-first candidate model is exactly where it would bite.

Each case declares:
  system / user   the real prompts, with fixture input
  parse           the task's own parser -> a list/dict of results
  expect_count    how many results a healthy run produces (the
                  "silently produces less" rule from CLAUDE.md)
  think           what production passes; None means "leave the model's default"
"""

import json

from agent.tools.research import RESEARCH_SYSTEM_PROMPT
from scribejay.calendar_colorizer import (
    CLASSIFY_SYSTEM_PROMPT,
    _classify_input,
    _parse_classification,
)
from scribejay.claude_time_blocks import BLURB_SYSTEM_PROMPT as BLOCK_BLURB_PROMPT
from tasks.daily_synthesis import (
    SYNTHESIS_SYSTEM_PROMPT,
    parse_nudges,
    render_candidates,
)
from tasks.opportunity_digest import (
    SCORING_SYSTEM_PROMPT,
    _compact_for_scoring,
    _parse_scores,
)
from tasks.starred_blurbs import BLURB_SYSTEM_PROMPT as REPO_BLURB_PROMPT, _first_line

# --------------------------------------------------------------------------- #
# Fixtures. Invented, but shaped and sized like the real thing — the point of
# the big scoring batch is that 40 leads is the size that broke.
# --------------------------------------------------------------------------- #

_COMPANIES = [
    ("Northwind Analytics", "VP of Engineering", "Portsmouth, NH"),
    ("Cedar Grove Health", "Director of Product", "Portland, ME"),
    ("Rivermark Logistics", "Head of Platform", "Boston, MA"),
    ("Blue Harbor Robotics", "Engineering Manager", "Nashua, NH"),
    ("Quarry Lane Software", "CTO", "Remote"),
    ("Third Coast Media", "VP Technology", "Manchester, NH"),
    ("Alder & Finch", "Director of Engineering", "Cambridge, MA"),
    ("Saltmarsh Energy", "Head of Data", "Dover, NH"),
    ("Pinehill Labs", "Principal Engineer", "Remote"),
    ("Copperfield Retail", "VP Product", "Concord, NH"),
]


def _lead(n):
    company, title, location = _COMPANIES[n % len(_COMPANIES)]
    return {
        "id": f"lever:{company.lower().replace(' ', '-')}:{n:04d}-aaaa-bbbb-cccc",
        "signal": "ats" if n % 3 else "edgar",
        "company": f"{company} {n // len(_COMPANIES) + 1}".strip(),
        "title": title,
        "location": location,
        "snippet": (f"{company} is hiring a {title}. The team is small and the "
                    "role owns delivery end to end, reporting to the founders."),
    }


_LEADS_10 = [_lead(n) for n in range(10)]
_LEADS_40 = [_lead(n) for n in range(40)]

_EVENTS = [
    "Volleyball", "Standup", "Dentist", "Lunch with Dana", "Deep work — Wren",
    "1:1 with Sam", "Grocery run", "Board meeting", "Physio", "Code review",
    "School pickup", "Evening Run",
]

_README = """# ripgrep

ripgrep is a line-oriented search tool that recursively searches the current
directory for a regex pattern. By default, ripgrep will respect gitignore rules
and automatically skip hidden files/directories and binary files. ripgrep has
first class support on Windows, macOS and Linux.

ripgrep is similar to other popular search tools like The Silver Searcher, ack
and grep. It is built on top of Rust's regex engine, which uses finite automata
to guarantee linear time searching.
"""

_TRANSCRIPT = """user: I want to add a retry to the synthesis call when the model returns nothing.
assistant: The empty-content case is the thinking budget being spent on scratchpad. A retry is the right shape here rather than think=False, because turning thinking off would make the nudges worse.
user: How many attempts?
assistant: Two. And warn even when the retry succeeds, otherwise the failure rate becomes invisible.
user: Do it, and add a test that pins the warning.
assistant: Added MAX_SYNTHESIS_ATTEMPTS = 2 with the warning on every empty attempt, plus a test asserting the WARNING fires on a successful retry.
"""

_CANDIDATES = [
    {"kind": "anchor", "overlap": ["mlx", "quantization"],
     "signal": {"kind": "read", "text": "MLX quantization formats explained"},
     "anchor": {"kind": "wiki page", "label": "Local model serving",
                "summary": "Notes on running quantized models on Apple silicon, "
                           "including MLX vs GGUF tradeoffs."}},
    {"kind": "anchor", "overlap": ["flask", "blueprint"],
     "signal": {"kind": "read", "text": "Structuring large Flask apps with blueprints"},
     "anchor": {"kind": "project", "label": "LocalLLMAgent",
                "summary": "Every feature API is a Flask blueprint."}},
    {"kind": "cross", "overlap": ["sourdough"],
     "signal": {"kind": "watched", "text": "Sourdough starter troubleshooting"},
     "other": {"kind": "read", "text": "Why my sourdough starter won't rise"}},
    {"kind": "anchor", "overlap": ["strava", "api"],
     "signal": {"kind": "read", "text": "Strava API rate limits"},
     "anchor": {"kind": "wiki page", "label": "Fitness data",
                "summary": "Pulling activities from Strava's v3 API."}},
]

_RESEARCH_INPUT = (
    "company: Northwind Analytics\n"
    "why_researching: posted a VP of Engineering role in Portsmouth NH\n"
    "form_d_filing_facts: {'total_offering': '4000000', 'date': '2026-06-02'}\n"
    "search_about: [{'summary': 'Northwind Analytics builds supply-chain "
    "forecasting software for mid-market distributors.'}, {'title': 'Northwind "
    "Analytics — Product', 'content': 'Demand forecasting and inventory "
    "planning for distributors with 50-500 employees.'}]\n"
    "search_people_funding: [{'title': 'Northwind raises $4M seed', 'content': "
    "'Founded by Dana Whitfield (CEO) and Marcus Ruiz (CTO). Seed round led by "
    "Granite Ventures. Roughly 30 employees.'}]\n"
    "search_news: [{'title': 'Northwind opens Portsmouth office', 'content': "
    "'The company opened a Portsmouth NH office and plans to double engineering "
    "headcount this year.'}]\n"
)

_RESEARCH_LABELS = [
    "What they do:", "Value proposition:", "Who to contact:", "Size & stage:",
    "Why now:", "Recent news:", "Red flags:",
]


def _parse_research(raw):
    """How many of the seven required section labels the brief actually has.
    The task itself only checks the brief is non-empty, so a half-filled
    template reaches the digest looking fine — which is the shape this scores."""
    return [label for label in _RESEARCH_LABELS if label.lower() in (raw or "").lower()]


def _parse_blurb(raw):
    line = _first_line(raw)
    return [line] if line else []


def _parse_block_blurb(raw):
    line = next((l.strip().strip('"').strip("*- ") for l in (raw or "").splitlines()
                 if l.strip()), "")
    return [line] if line else []


# A hand-written ideal answer per case. Not sent to any model — it's what
# tests/test_run_eval.py feeds each parser to prove the parser, the fixture and
# expect_count are wired together correctly.
#
# This exists because the wiring broke twice while the harness was being built,
# and both times a perfectly good model answer was scored as a failure. A
# harness bug and a model weakness look identical in the results table, and the
# harness bug is the one that costs hours of Ollama time to discover.
_GOLDEN_SCORES_10 = "\n".join(f"{n}|{5 + n % 5}|Interim leadership angle." for n in range(1, 11))
_GOLDEN_SCORES_40 = "\n".join(f"{n}|{5 + n % 5}|Interim leadership angle." for n in range(1, 41))
_GOLDEN_COLORS = json.dumps({str(n): "1" for n in range(1, len(_EVENTS) + 1)})
_GOLDEN_BRIEF = "\n".join(f"{label} Something factual." for label in _RESEARCH_LABELS)


CASES = [
    {
        "id": "digest_scoring_10",
        "task": "opportunity_digest",
        "system": SCORING_SYSTEM_PROMPT,
        "user": f"leads: {json.dumps(_compact_for_scoring(_LEADS_10))}",
        # The RAW leads, as production passes: _parse_scores maps a batch
        # position back to a lead id, and the compacted form has no id.
        "parse": lambda raw: _parse_scores(raw, _LEADS_10),
        "golden": _GOLDEN_SCORES_10,
        "expect_count": 10,
        "think": False,
    },
    {
        "id": "digest_scoring_40",
        # 40 is MAX_SCORE_ITEMS and the size that emailed 11 leads unscored.
        "task": "opportunity_digest",
        "system": SCORING_SYSTEM_PROMPT,
        "user": f"leads: {json.dumps(_compact_for_scoring(_LEADS_40))}",
        "parse": lambda raw: _parse_scores(raw, _LEADS_40),
        "golden": _GOLDEN_SCORES_40,
        "expect_count": 40,
        "think": False,
    },
    {
        "id": "calendar_colorizer",
        "task": "calendar_colorizer",
        "system": CLASSIFY_SYSTEM_PROMPT,
        "user": json.dumps(_classify_input([{"summary": s} for s in _EVENTS])),
        # _parse_classification RAISES on empty or non-JSON; the runner catches
        # that and records it as a parse failure, which is the real behaviour.
        "parse": _parse_classification,
        "golden": _GOLDEN_COLORS,
        "expect_count": len(_EVENTS),
        "think": False,
    },
    {
        "id": "starred_blurb",
        "task": "starred_blurbs",
        "system": REPO_BLURB_PROMPT,
        "user": f"repo: BurntSushi/ripgrep\nREADME:\n{_README}",
        "parse": _parse_blurb,
        "golden": "ripgrep is a fast recursive regex search tool.",
        "expect_count": 1,
        "think": False,
    },
    {
        "id": "time_block_blurb",
        "task": "claude_time_blocks",
        "system": BLOCK_BLURB_PROMPT,
        "user": f"projects: LocalLLMAgent\n\ntranscript:\n{_TRANSCRIPT}\n",
        "parse": _parse_block_blurb,
        "golden": "Added a retry for empty synthesis responses.",
        "expect_count": 1,
        "think": False,
    },
    {
        "id": "research_brief",
        "task": "research",
        "system": RESEARCH_SYSTEM_PROMPT,
        "user": _RESEARCH_INPUT,
        "parse": _parse_research,
        "golden": _GOLDEN_BRIEF,
        "expect_count": 7,
        "think": False,
    },
    {
        "id": "daily_synthesis",
        # The one case that runs with thinking ON, because production does —
        # this is where the empty-content risk lives, and where a reasoning-heavy
        # candidate model is most likely to differ.
        "task": "daily_synthesis",
        "system": SYNTHESIS_SYSTEM_PROMPT,
        "user": render_candidates(_CANDIDATES),
        "parse": parse_nudges,
        "golden": "- You read about MLX and your serving note covers it.",
        # Silence is a legitimate answer here (the model may judge nothing
        # genuine), so this is scored on non-empty CONTENT, not on nudge count.
        "expect_count": None,
        "think": None,
    },
]
