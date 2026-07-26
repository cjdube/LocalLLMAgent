"""Deterministic text checks a lens can opt into, computed in Python and handed
to the model as fact.

Some of what a prose lens wants is counting and exact matching, not judgement:
"is there a sentence with two or more em dashes", "does the phrase 'paradigm
shift' appear". Asking a small local model to do that inside a ten-pattern rubric
failed in both directions, measured on the ai-slop lens: an em-dash rule and an
exact-phrase list fired in only 1 of 3 runs when buried in the pattern list, and
once pushed to the top they crowded out other findings and eventually produced a
*fabricated* one — the model quoted a comma-heavy sentence containing no em
dashes at all and called it a cluster. Counting is Python's job (CLAUDE.md).

A lens opts in through its own frontmatter, so no lens is named here and
`evaluate_against` stays generic:

    ---
    lens: true
    description: ...
    max_em_dashes_per_sentence: 1
    banned_phrases: paradigm shift, cutting-edge, game changer
    ---

Either key alone is enough; a lens with neither gets no checks block and behaves
exactly as before. render_checks_block() reports every enabled check including
the ones that found nothing — "none found" is the half that prevents the
fabrication, since the model can't claim em dashes the code says aren't there.
"""

import re

EM_DASH = "—"

# Bound the block so a pathological target can't crowd out the target itself.
_MAX_REPORTED = 10
_MAX_QUOTE_CHARS = 300

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
# Markdown line breaks end a sentence as surely as a period does — a bullet or
# heading often has no terminal punctuation at all.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def _frontmatter_values(lens_text: str) -> dict:
    """The lens's frontmatter as raw {key: value} strings, or {} if it has none."""
    m = _FRONTMATTER_RE.match(lens_text or "")
    if not m:
        return {}
    values = {}
    for line in m.group(1).splitlines():
        key, sep, val = line.partition(":")
        if sep:
            values[key.strip().lower()] = val.strip()
    return values


def checks_config(lens_text: str) -> dict:
    """Which deterministic checks this lens asks for. Empty dict means none, and
    an unparseable value is treated as absent rather than raising — a malformed
    lens must not break an evaluation."""
    values = _frontmatter_values(lens_text)
    config = {}

    raw_max = values.get("max_em_dashes_per_sentence", "")
    if raw_max:
        try:
            config["max_em_dashes_per_sentence"] = int(raw_max)
        except ValueError:
            pass

    phrases = [p.strip() for p in values.get("banned_phrases", "").split(",")]
    phrases = [p for p in phrases if p]
    if phrases:
        config["banned_phrases"] = phrases

    return config


def em_dash_sentences(text: str, limit: int) -> list[str]:
    """Sentences containing more than `limit` em dashes, in document order."""
    hits = []
    for sentence in _SENTENCE_SPLIT.split(text or ""):
        s = sentence.strip()
        if s.count(EM_DASH) > limit:
            hits.append(s[:_MAX_QUOTE_CHARS])
    return hits


def banned_phrases_found(text: str, phrases: list[str]) -> list[str]:
    """Which of `phrases` appear in `text`, case-insensitively, deduped and in
    the order they were declared."""
    low = (text or "").lower()
    return [p for p in phrases if p.lower() in low]


def render_checks_block(text: str, config: dict) -> str:
    """The authoritative findings block injected ahead of the target, or "" when
    the lens asked for no checks. Reports "none" for a check that passed as
    deliberately as it reports a hit."""
    if not config:
        return ""

    lines = []

    limit = config.get("max_em_dashes_per_sentence")
    if limit is not None:
        hits = em_dash_sentences(text, limit)
        if hits:
            lines.append(f"- Sentences with more than {limit} em dash(es): "
                         f"{len(hits)} found.")
            for h in hits[:_MAX_REPORTED]:
                lines.append(f'    - "{h}"')
        else:
            lines.append(f"- Sentences with more than {limit} em dash(es): none. "
                         f"There is no em-dash finding to report.")

    phrases = config.get("banned_phrases")
    if phrases:
        found = banned_phrases_found(text, phrases)
        if found:
            # One line per phrase, mirroring the em-dash shape. Listed together on
            # a single line the model forwarded only one of two — the same
            # grouped-items blindness that made the phrase list need splitting out
            # of the single-word list in the first place.
            lines.append(f"- Banned phrases present: {len(found)} found.")
            for p in found[:_MAX_REPORTED]:
                lines.append(f'    - "{p}"')
        else:
            lines.append("- Banned phrases from the lens's list: none. There is no "
                         "banned-phrase finding to report.")

    return (
        "Mechanical checks already run in code against the target below. These "
        "results are authoritative and complete — report the ones that found "
        "something, and do not re-derive, contradict, or extend them:\n"
        + "\n".join(lines)
    )
