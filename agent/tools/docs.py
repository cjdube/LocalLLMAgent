"""Read-only access to the documentation of the THREE systems that make up how
Wren works, so she can answer "how do you handle a scheduled task that fails"
and "how do my notes end up in the wiki" out of the files that decided it.

Everything about how Wren works is written down — README.md, ANALYSIS.md and the
files under docs/ — and until this tool existed none of it reached the model at
runtime. agent/tools/projects.py comes closest and still does not get there: it
reads this repo's README on the nightly scan, then _merge() strips the body
before anything model-facing sees it, leaving a one-line distilled summary.

The same was true one repo further out, and it mattered more. The learnings wiki
is the knowledge base Wren queries constantly (agent/tools/wiki.py), but HOW a
note becomes a page, and how the wiki is kept tidy, is documented in
ObsidianWikiAgent. How the record she reads gets WRITTEN is documented in
ScribeJay. Both were unreachable, and docs/scribejay.md forbids the obvious
workaround: "How ScribeJay works is ScribeJay's own docs/architecture.md — do
not describe its internals here, or the two copies drift and the wrong one gets
read." This reader is what lets that rule hold.

THE SEAM IS NOT WEAKENED
------------------------
A sibling's Markdown is read as DATA — the same category as its logs and its
plists. Nothing here imports a sibling, shells into one, or writes to one. The
coupling is one-way and is exactly "open a .md file".

THREE CORPORA, ONE NAMESPACE
----------------------------
Wren's own documents keep BARE names — 'readme', 'limits', 'module-map' — so
nothing that resolved before this change stopped resolving. A sibling's carry
its repo as a prefix: 'scribejay/timezones', 'wiki/agent-context'.

The prefix is not decoration. EIGHT ScribeJay documents share a filename stem
with one of Wren's (llm-backend, logs, model-constraints, ntfy-setup,
opaque-identifiers, readme, timezones, usage-ledger) and ObsidianWikiAgent
shares one (readme). A flat merge would setdefault() the sibling away, and Wren
would report in good faith that ScribeJay has no timezone document while reading
Wren's own.

A prefix was chosen over a second `repo` argument on read_doc. An argument
creates a cross-argument consistency requirement the small model has to satisfy
twice; a prefix makes the wrong combination unrepresentable — there is one
string, and the model copies it back the way it already copies 'module-map'.

WHAT IS IN THE CORPUS, AND WHAT IS DELIBERATELY NOT
----------------------------------------------------
In, from THIS repo: README.md (what she can do), ANALYSIS.md (how the subsystems
fit), and the top level of docs/ (why each limit and design is what it is).

In, from ObsidianWikiAgent: README.md, SECURITY.md (the trust boundary the vault
sits behind) and docs/ — of which docs/agent-context.md is the point of
including the repo at all, being the descriptive, tool-neutral rationale behind
its ingest and write paths.

In, from ScribeJay: README.md and docs/ — of which docs/architecture.md is the
file docs/scribejay.md defers to.

Out, and none of these are oversights:

  AGENTS.md      Imperative instructions addressed to CODING agents working on
                 a repo — "run pytest before calling a change done", "commit
                 straight to main". Wren is not that agent. Handing her a file of
                 orders aimed at someone else is noise at best, and at worst she
                 reads a maintenance instruction as something she should do. This
                 is WORSE for a sibling's AGENTS.md, which is another repo's
                 maintenance contract entirely. ANALYSIS.md and the wiki
                 engine's docs/agent-context.md are in precisely because they are
                 the descriptive counterparts: they explain the same systems
                 without telling anyone to change anything.
  CLAUDE.md      An import-only pointer in all three repos. Excluded by
                 construction — root files are NAMED, never globbed.
  persona.md     ScribeJay's scribejay/persona.md is another AGENT'S
                 system-prompt material. Wren adopting ScribeJay's voice because
                 a search hit landed it in her context is a failure with no error
                 message. Outside both docs/ and the root list, so excluded by
                 construction.
  lint_defects/  ObsidianWikiAgent's tools/lint_defects/ holds deliberately
                 FABRICATED defective wiki pages — fixtures for its linter.
                 Feeding them to Wren would put invented wiki claims in the
                 corpus she is told to trust. Excluded by construction.
  docs/reviews/  Gitignored in all three repos. Audit plans and findings, not
                 documentation.
  docs/handoff/  Gitignored. Work belonging to sibling repos.

The corpus is read fresh from disk on every call. There is no index to keep in
sync and nothing to invalidate: ~740KB reads in single-digit milliseconds, and
the wiki's search does the same over more than that (agent/tools/wiki.py).

WHERE THE ROOTS COME FROM
-------------------------
Wren's own root is derived from __file__: the docs travel with the code, there
is no deployment in which they are somewhere else, and a setting nobody can
meaningfully change is a setting that can be set wrong.

The two siblings are the opposite case. They are separate checkouts that can be
moved, unmounted, or simply never cloned, so each gets a Setting row read at
CALL time (so a /settings save lands without a restart). A root that is not a
directory contributes no documents at all — Wren-only, never an error, because
"ScribeJay is mid-upgrade" must not break a question about Wren.

This mirrors the SHAPE of agent/tools/wiki.py — search returns rows, read returns
one trimmed document — without importing it. The two corpora agree on nothing
else: wiki pages carry ObsidianWikiAgent's frontmatter, a `**Summary**:` line and
a `## Related pages` footer, and none of that exists here. Sharing the helpers
would mean a change to that project's page format could silently break this.

Usage:
    python -m agent.tools.docs search "scheduled task failure"
    python -m agent.tools.docs search "how do notes get into the wiki"
    python -m agent.tools.docs read limits
    python -m agent.tools.docs read limits --section "context window"
    python -m agent.tools.docs read scribejay/architecture
    python -m agent.tools.docs list
"""

import argparse
import re
import sys
from pathlib import Path

from agent import config, prefs
from agent.tools._http import print_result

_NAME = prefs.user_name()

# agent/tools/docs.py -> agent/tools -> agent -> the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Root-level files that describe the system. AGENTS.md is excluded on purpose —
# see the module docstring.
_ROOT_DOCS = ("README.md", "ANALYSIS.md")

# Only the top level of docs/. The two subdirectories are gitignored and neither
# is documentation; iterdir() rather than rglob() is what keeps them out, so
# switching to a recursive walk would quietly pull both back in.
_DOCS_DIR = "docs"

# (name prefix, root-level files to include). The top level of docs/ is always
# included on top of these. Root files are NAMED, never globbed — that is what
# keeps each repo's AGENTS.md and CLAUDE.md out by construction rather than by a
# rule somebody has to remember to hold.
_SIBLING_REPOS = (
    ("wiki", ("README.md", "SECURITY.md")),
    ("scribejay", ("README.md",)),
)

# search_docs' caps, sized the way search_wiki's are. Summaries run ~200 chars
# and the corpus is 64 documents across three repos, so the CHAR budget is now
# the one that normally binds — the broadest query keeps 13 of the 15 allowed
# rows before the char cap cuts it. Both still sit well under the flat 8000-char
# tool-result cap in agent/loop.py (the worst measured case is 5,190), which is
# why this tool needs no TOOL_RESULT_CHAR_CAPS entry of its own. A repo prefix
# adds ~10 chars to a row, so the caps bound the PAGE, not the corpus: the
# corpus can grow again without either number moving.
MAX_SEARCH_RESULTS = 15
MAX_SEARCH_CHARS = 4000

# How much of a document's body search_docs quotes around a body-only hit. The
# row's name and summary cannot explain a body match — that is what "body match"
# means — so this snippet is the only evidence of why the document came back.
MAX_CONTEXT_CHARS = 200

# read_doc's own budget, under the 14000-char backstop agent/loop.py gives this
# tool. The gap is escaping headroom: the backstop counts the JSON-escaped
# result, and markdown's newlines roughly double on the way through. Widen both
# together or the blind backstop re-cuts a document _fit_doc trimmed on purpose.
MAX_DOC_CHARS = 12000

# Room _fit_doc holds back for its "here is what I cut" notice. The boilerplate
# is ~340 chars; the rest covers the dropped section names, and the biggest
# document here has 20 of them.
_NOTICE_RESERVE = 900

# How much of the opening paragraph becomes the summary. Long enough to say what
# a document is for, short enough that 15 rows fit the search budget.
_SUMMARY_CHARS = 220

# Scoring weights, highest signal first. The document NAMED for a topic is more
# often that topic's document than one whose summary mentions it, which in turn
# beats one that merely says the word somewhere in its body.
_NAME_WEIGHT, _SUMMARY_WEIGHT, _BODY_WEIGHT = 3, 2, 1

_H1_RE = re.compile(r"^# .*$\n?", re.MULTILINE)
_H2_RE = re.compile(r"^## (.+?)\s*$", re.MULTILINE)

# Query terms. Splitting on non-alphanumerics folds punctuation and the hyphens
# in document names ('module-map') into the same shape.
_TERM_RE = re.compile(r"[a-z0-9]+")

# Only _match_section uses these, and only in its last-resort overlap branch.
# That branch drops words under 4 chars as a cheap stand-in for "not a
# stopword", and the proxy leaks: 'that', 'here' and 'with' are all 4 letters.
# Measured — asking for the section "something that is not a heading here"
# returned "Limits that are not about the model", on the shared 'that' alone.
# A wrong section is worse than no section: the model reads it, answers from
# it, and nothing tells either of them it got the wrong part. The miss instead
# returns the list of real headings, which it can retry against.
_STOPWORDS = frozenset({
    "that", "this", "these", "those", "there", "here", "with", "from", "what",
    "when", "where", "which", "does", "your", "about", "into", "they", "them",
    "then", "than", "have", "been", "will", "would", "should", "some",
    "something", "anything", "thing", "things", "just", "only", "also",
})

# Fenced code blocks are stripped before a summary is taken, never before body
# scoring. A document that opens with a shell snippet would otherwise summarise
# as that snippet; but the code in these files is a real answer to "what command
# does X", so search still has to see it.
_FENCE_RE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def _sibling_roots() -> dict:
    """{prefix: configured root path} for the two sibling checkouts.

    Read at CALL time, not at import, so a /settings save reaches the next
    question without a restart.

    The two reads are written out LONGHAND — one string-literal key each —
    rather than folded into _SIBLING_REPOS and read in a loop. That is not
    style: tests/test_schema.py's drift guard resolves a config.getenv() key
    only when it is a string literal or a module-level constant, and a key that
    arrives as a LOOP VARIABLE is invisible to it. Folded into the table, the
    guard that exists to catch a key with no Setting row caught neither of these
    — measured, _tree_reads() returned both only after this shape.
    """
    return {
        "wiki": config.getenv("WREN_WIKI_REPO_PATH", "~/Projects/ObsidianWikiAgent"),
        "scribejay": config.getenv("WREN_SCRIBEJAY_REPO_PATH", "~/Projects/ScribeJay"),
    }


def _md_files(root: Path, root_docs: tuple) -> list:
    """(stem, path) for one repo's corpus: the NAMED root files, then the top
    level of docs/.

    iterdir() and never rglob(). All three repos gitignore docs/reviews/ and this
    one also has docs/handoff/; neither is documentation, and a switch to a
    recursive walk would quietly pull both back in for every repo at once.
    """
    out = []
    for filename in root_docs:
        path = root / filename
        if path.is_file():
            out.append((path.stem.lower(), path))

    docs_dir = root / _DOCS_DIR
    if docs_dir.is_dir():
        for path in sorted(docs_dir.iterdir()):
            if path.is_file() and path.suffix == ".md" and not path.name.startswith("."):
                out.append((path.stem.lower(), path))
    return out


def _doc_paths() -> dict:
    """{name: path} for every document in the corpus.

    Wren's own documents keep BARE names — 'readme', 'analysis', 'limits',
    'module-map'. A sibling's carry its repo as a prefix — 'scribejay/timezones',
    'wiki/agent-context' — because eight ScribeJay stems collide with one of
    Wren's and a flat merge would setdefault() the sibling away. See the module
    docstring for the full list and for why a prefix beat a `repo` argument.

    Building the map up front is still what makes read_doc safe, and the prefix
    does not weaken it: a key is CONSTRUCTED here from a prefix we chose and a
    stem read off disk, never parsed out of model input, and nothing joins a
    model-supplied name onto a path. A name that is not already a key simply does
    not resolve, so there is no traversal to defend against.

    A sibling root that is not a directory contributes nothing — Wren-only, never
    an error. A checkout can be moved, unmounted, or never cloned, and that means
    "no ScribeJay documents today", not a failed tool call.
    """
    paths = {}
    # setdefault still earns its place within a repo: it is what makes a
    # root-level README.md win over a docs/readme.md rather than the walk order
    # deciding. Across repos it is the prefix, not this, that stops a collision.
    for stem, path in _md_files(_REPO_ROOT, _ROOT_DOCS):
        paths.setdefault(stem, path)

    roots = _sibling_roots()
    for prefix, root_docs in _SIBLING_REPOS:
        root = Path(roots[prefix]).expanduser()
        if not root.is_dir():
            continue
        for stem, path in _md_files(root, root_docs):
            paths.setdefault(f"{prefix}/{stem}", path)
    return paths


def _summary(text: str) -> str:
    """The document's opening paragraph, trimmed to _SUMMARY_CHARS.

    Every document in this corpus opens the same way — an H1 naming it, then one
    paragraph saying what it is and why it exists — so the convention is worth
    reading rather than a `**Summary**:` line worth adding. A document that
    breaks the convention degrades to a first paragraph that is merely less
    useful, never to an error.
    """
    body = _H1_RE.sub("", _FENCE_RE.sub("", text), count=1)
    for block in body.split("\n\n"):
        para = " ".join(block.split())
        # '<' is in the list for a raw HTML tag: ScribeJay's README opens with
        # an <img> badge line ABOVE its H1, and _H1_RE strips the H1 from
        # anywhere, so without this the summary would be the tag. Skipping a tag
        # block is the same shape as skipping a table row or a blockquote — no
        # HTML stripping, which would break the docstring's contract that a
        # document breaking the convention degrades to a less useful first
        # paragraph, never to an error.
        if not para or para.startswith(("#", "|", ">", "-", "*", "<")):
            continue
        if len(para) <= _SUMMARY_CHARS:
            return para
        return para[:_SUMMARY_CHARS].rsplit(" ", 1)[0] + " …"
    return ""


def _doc_texts() -> list:
    """Every document as {name, summary, body}, read whole.

    The H1 is dropped from `body` because it restates the filename, which is
    already scored and scored higher; double-scoring it would let a title hit
    outrank a real body hit. The summary is left in the body on purpose, unlike
    the wiki's reader: here it is an ordinary first paragraph carrying real
    prose, not a separate one-line field, and cutting it would lose text that
    legitimately answers questions.
    """
    rows = []
    for name, path in _doc_paths().items():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        rows.append({"name": name, "summary": _summary(text), "body": _H1_RE.sub("", text, count=1)})
    return rows


def _context(body: str, match: re.Match) -> str:
    """MAX_CONTEXT_CHARS of body centred on a hit, whitespace collapsed.
    Ellipses mark a window cut out of more text, so a fragment cannot read as a
    whole sentence."""
    half = max(0, (MAX_CONTEXT_CHARS - len(match.group(0))) // 2)
    start, end = max(0, match.start() - half), min(len(body), match.end() + half)
    snippet = " ".join(body[start:end].split())
    return ("… " if body[:start].strip() else "") + snippet + (" …" if body[end:].strip() else "")


def _split_sections(text: str) -> list:
    """(heading, section text including its heading) for each H2, in order.
    Empty when a document has no H2s at all, which is why sections are a
    fallback for the documents that overflow rather than how every one is read.
    """
    heads = [(m.group(1).strip(), m.start()) for m in _H2_RE.finditer(text)]
    return [
        (name, text[start: heads[i + 1][1] if i + 1 < len(heads) else len(text)].rstrip())
        for i, (name, start) in enumerate(heads)
    ]


def _heading_terms(text: str) -> set:
    """The words in `text` worth matching a heading on."""
    return {t for t in _TERM_RE.findall(text.lower())
            if len(t) >= 4 and t not in _STOPWORDS}


def _match_section(sections: list, wanted: str):
    """The section whose heading best matches `wanted`; None if nothing is close.

    Deliberately forgiving, for the same reason the wiki's is: the model is
    re-typing a heading it read in a trim notice, and these headings are long
    English phrases ("Tests must never touch production state"). An exact-match
    lookup would turn a dropped word into a dead end. Exact, then either
    direction of substring, then best word overlap.
    """
    want = wanted.strip().lower()
    for heading, body in sections:
        if heading.lower() == want:
            return heading, body

    # Longest wins, not first: short headings like "Limits" are a substring of
    # half the phrasings a model might type, and document order would hand one
    # back ahead of the specific section actually asked for.
    hits = [(h, b) for h, b in sections if want in h.lower() or h.lower() in want]
    if hits:
        return max(hits, key=lambda hb: len(hb[0]))

    # Short words and stopwords are both excluded, because these headings are
    # English phrases: one shared "and" or "that" would otherwise count as a
    # whole match. See the _STOPWORDS comment for the case that proved it.
    terms = _heading_terms(want)
    best, best_score = None, 0
    for heading, body in sections:
        score = len(terms & _heading_terms(heading))
        if score > best_score:
            best, best_score = (heading, body), score
    return best


def _fit_doc(text: str, budget: int = MAX_DOC_CHARS) -> str:
    """`text` trimmed to `budget`, saying what it cut and which sections went.

    The notice is the point, more than the trim. Handed a silently truncated
    document, the model treats what it got as the whole thing and reports that
    the documentation does not cover something that is sitting in the part it
    never saw — the exact false negative the wiki's reader was built to stop,
    and 13 of these documents are over the budget. So the notice names the
    dropped headings and tells her not to make that claim.
    """
    if len(text) <= budget:
        return text

    kept = text[: max(0, budget - _NOTICE_RESERVE)]

    # A section counts as unread unless it ENDS inside the kept text. Checking
    # that its heading survived is not enough: the cut usually lands mid-section,
    # leaving the heading visible and its content gone, which would report the
    # section as read and reintroduce the false negative this notice prevents.
    heads = [(m.group(1), m.start()) for m in _H2_RE.finditer(text)]
    ends = [heads[i + 1][1] if i + 1 < len(heads) else len(text) for i in range(len(heads))]
    dropped = [name for (name, _), end in zip(heads, ends) if end > len(kept)]

    notice = (
        f"\n\n[Only the first {len(kept)} of {len(text)} characters of this "
        "document are shown; the rest did not fit."
        + (f" Not shown in full: {', '.join(dropped)}. Call read_doc again with "
           "the section argument set to one of those headings to read it in "
           "full." if dropped else "")
        + " Do NOT say this document or the documentation lacks something — you "
        "have not read all of it.]"
    )
    return kept + notice


def _rank_key(score: int, name: str) -> tuple:
    """The result ordering: score first, then Wren's own document, then name.

    Wren's OWN document goes ahead of a sibling's on a tie because the question
    was asked of Wren — and eight ScribeJay documents share a filename stem with
    one of hers, so a tie is the common case here, not the edge. Left to the
    name alone the ALPHABET decided it: the key was (-score, name), 's' sorts
    before 't', and 'scribejay/timezones' beat 'timezones'. Measured on the real
    corpus, the flip hit 'timezones' and 'usage ledger'.

    The name stays last, so the order is still stable run to run.

    This is a named function rather than an inline tuple so a test can put the
    old two-part key back and prove the tie-break is what decides the order —
    tests/test_docs.py:test_the_own_first_tie_break_bites.
    """
    return (-score, "/" in name, name)


def _search_docs(query: str) -> list:
    """Documents matching `query` as {name, summary} rows — plus a `context`
    snippet on the ones that matched in the body — best match first.

    Name and summary terms match as SUBSTRINGS, because names are slugs
    ('module-map', 'opaque-identifiers') and a term has to be able to match
    inside one. Body terms match on WORD BOUNDARIES, because a body is prose,
    where substring matching is actively wrong: it is what would rank
    `model-constraints` for the 'we' inside 'power'. A term scores once per
    document however often it appears, so a 30KB document cannot outrank a
    short one on repetition alone. Ties break on the name, so the order is
    stable run to run.
    """
    terms = _TERM_RE.findall(query.lower())
    if not terms:
        return []
    body_patterns = [re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE) for t in terms]
    scored = []
    for row in _doc_texts():
        name, summary, body = row["name"].lower(), row["summary"].lower(), row["body"]
        score = sum(_NAME_WEIGHT for t in terms if t in name)
        score += sum(_SUMMARY_WEIGHT for t in terms if t in summary)
        # The EARLIEST body hit, not the first query term's, so the snippet
        # shows where the document starts talking about this rather than
        # wherever the user happened to put a word in the question.
        hits = [m for m in (p.search(body) for p in body_patterns) if m]
        score += _BODY_WEIGHT * len(hits)
        if not score:
            continue
        out = {"name": row["name"], "summary": row["summary"]}
        if hits:
            out["context"] = _context(body, min(hits, key=lambda m: m.start()))
        scored.append((_rank_key(score, row["name"]), out))
    scored.sort(key=lambda pair: pair[0])
    return [row for _, row in scored]


# --- model-facing tools ---

def search_docs(query: str) -> dict:
    """Documents matching `query`, as rows read_doc can be called on. Capped;
    when the cap bites, `truncated` says so rather than dropping matches
    silently, so the model can narrow instead of assuming it saw everything."""
    if not query or not query.strip():
        return {"error": "query must not be empty"}
    matches = _search_docs(query.strip())
    kept, total = [], 0
    for row in matches[:MAX_SEARCH_RESULTS]:
        kept.append(row)
        total += len(row["name"]) + len(row["summary"]) + len(row.get("context", ""))
        if total > MAX_SEARCH_CHARS:
            break
    result = {"matches": kept}
    if len(kept) < len(matches):
        result["truncated"] = (
            f"showing the {len(kept)} best of {len(matches)} matching documents — "
            "use a narrower query to see the rest"
        )
    return result


def read_doc(name: str, section: str | None = None) -> dict:
    """One document from the corpus, by a name search_docs returned."""
    if not name or not name.strip():
        return {"error": "name must not be empty"}

    key = name.strip().lower()
    if key.endswith(".md"):
        key = key[:-3]
    paths = _doc_paths()
    path = paths.get(key)
    if path is None:
        # The model re-types what it read, and the repo prefix is the likeliest
        # thing it drops — 'architecture' for 'scribejay/architecture'. Resolve a
        # bare stem only when exactly ONE document ends in it. This is still a
        # lookup against keys already in the map, never a path join, so it adds
        # no traversal surface. A bare name that IS one of Wren's own matched
        # exactly above and never reaches here: bare means Wren's.
        hits = [k for k in paths if k.endswith("/" + key)]
        if len(hits) == 1:
            key, path = hits[0], paths[hits[0]]
        elif hits:
            return {"error": f"more than one document is called '{name}' — "
                             "ask for one by its full name",
                    "documents": sorted(hits)}
    if path is None:
        # Name what exists rather than only saying no, so the retry is informed.
        return {"error": f"no document called '{name}'", "documents": sorted(paths)}

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return {"error": f"could not read '{name}': {e}"}

    if section is None or not section.strip():
        return {"document": key, "content": _fit_doc(text)}

    sections = _split_sections(text)
    if not sections:
        return {"error": f"document '{key}' has no sections — read it without a section argument"}
    hit = _match_section(sections, section)
    if hit is None:
        return {"error": f"no section like '{section}' in document '{key}'",
                "sections": [heading for heading, _ in sections]}
    heading, body = hit
    return {"document": key, "section": heading, "content": _fit_doc(body)}


def list_docs() -> dict:
    """Human-only: the corpus, for checking what is in and out of it. Not
    registered as a tool — search_docs is the model's entry point, for the
    reason the wiki's listing tools are unregistered."""
    return {"documents": sorted(_doc_paths())}


# Written to the rule in AGENTS.md: a tool that answers "what does the
# documentation say?" must state that the answer is NOT in the model's head.
# Without it the model supplies a confident, plausible answer and never calls
# the tool — and this corpus is the worst case for that, because its topics
# (agents, scheduling, context limits) are exactly what a model can invent
# authoritative-sounding prose about. list_games went from 2-of-12 replays
# fabricating entries to 12 of 12 calling the tool on this wording alone.
SEARCH_DOCS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_docs",
        "description": (
            "Search the FULL TEXT of the documentation for the three systems "
            "that make up how Wren works: her OWN files (how she is built, what "
            "she can do, how each scheduled task behaves, where her limits are "
            "and why each one was chosen); the WIKI ENGINE that files raw notes "
            "into the learnings wiki she reads and keeps it tidy; and "
            "SCRIBEJAY, the journaling agent that writes the record of the day "
            "she reads back. Get back each matching document name with a "
            "one-line summary, plus a short quote when the match was in the "
            f"body. Use this for any question {_NAME} asks about how Wren "
            "works, how his notes get into the wiki or how it is maintained, "
            "how the record of his day gets written, why something is done a "
            "particular way, or what happens in some situation. **This is the "
            "ONLY way to find out what any of that documentation says: you do "
            "not know its contents.** Only the documents this tool returns "
            "exist. Never name, describe, or quote a document that did not come "
            "back from a search. Call this first, then call read_doc on the one "
            "you want. If it returns no matches, say the documentation does not "
            "cover that — do not answer from your own knowledge as if it came "
            "from those docs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Words to look for, matched against document names, "
                        "summaries and bodies, e.g. 'scheduled task failure'. "
                        "Prefer two or three content words; drop filler like "
                        "'how do you handle', which matches nothing useful."
                    ),
                },
            },
            "required": ["query"],
        },
    },
}

READ_DOC_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_doc",
        "description": (
            "Read one document, by a name search_docs returned. Cite the "
            "document name in your answer. **A name with a slash in it says "
            "which system the document belongs to**: 'scribejay/architecture' "
            "is ScribeJay's, 'wiki/agent-context' is the wiki engine's, and a "
            "plain name with no slash is Wren's own. Pass the name back exactly "
            "as the search returned it — several documents share a plain name "
            "across the three systems, and dropping the part before the slash "
            "gets you Wren's copy instead. Several are too long to return "
            "whole; those come back with a note naming the sections that were "
            "cut. When the answer you need is in one of those, call this again "
            "with that heading as `section` to read that part in full."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Document name exactly as search_docs returned it — "
                        "'limits', 'readme', 'scribejay/timezones', "
                        "'wiki/agent-context' (with or without .md)."
                    ),
                },
                "section": {
                    "type": "string",
                    "description": (
                        "Optional. A section heading from the document, e.g. "
                        "'Scheduled tasks'. Returns just that section. Use only "
                        "a heading the document itself named; omit to read the "
                        "document."
                    ),
                },
            },
            "required": ["name"],
        },
    },
}

DOCS_TOOL_SCHEMAS = [
    SEARCH_DOCS_SCHEMA,
    READ_DOC_SCHEMA,
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p_search = sub.add_parser("search")
    p_search.add_argument("query")
    p_read = sub.add_parser("read")
    p_read.add_argument("name")
    p_read.add_argument("--section", default=None)
    args = parser.parse_args()

    if args.cmd == "list":
        result = list_docs()
    elif args.cmd == "search":
        result = search_docs(args.query)
    else:
        result = read_doc(args.name, args.section)

    return print_result(result)


if __name__ == "__main__":
    sys.exit(main())
