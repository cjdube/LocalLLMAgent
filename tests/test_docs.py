"""Tests for agent/tools/docs.py.

These run against the REAL corpus — this repo's own README.md, ANALYSIS.md and
docs/ — rather than a fixture tree. That is deliberate and it is the point: the
corpus is not user data that varies by machine, it is files that ship with the
checkout, so a fixture would only prove the code can read a fixture. The
failures worth catching are "AGENTS.md crept back in" and "a 28KB document comes
back silently truncated", and neither is visible against invented files.

Where a test needs a shape the real corpus does not have — a document with no
sections, a body crafted to prove a scoring rule — it builds that string
directly and calls the helper, instead of writing files into the repo.
"""

import json

from agent.tools import docs


# ---- the corpus boundary ---------------------------------------------------

def test_agents_md_is_not_in_the_corpus():
    """AGENTS.md is imperative instructions addressed to CODING agents — "run
    pytest before calling a change done", "commit straight to main". Wren is not
    that agent. The risk is not noise, it is that she reads an order aimed at
    someone else as one aimed at her."""
    assert "agents" not in docs.list_docs()["documents"]


def test_the_gitignored_subdirectories_stay_out():
    """docs/reviews/ is audit plans and docs/handoff/ is work belonging to
    sibling repos. Both are gitignored; neither is documentation. iterdir() is
    what keeps them out, so a switch to rglob() would quietly pull both back in
    and this is what would catch it."""
    names = set(docs.list_docs()["documents"])
    for path in (docs._REPO_ROOT / "docs").iterdir():
        if path.is_dir():
            for inner in path.glob("*.md"):
                assert inner.stem.lower() not in names, \
                    f"{inner} is inside a gitignored subdirectory and reached the corpus"


def test_the_readme_and_analysis_are_in_the_corpus():
    """The two root-level documents. README says what she can do; ANALYSIS says
    how the subsystems fit. ANALYSIS is in precisely because it is the
    DESCRIPTIVE counterpart to AGENTS.md — it explains the system without
    telling anyone to change it."""
    names = docs.list_docs()["documents"]
    assert "readme" in names
    assert "analysis" in names


def test_every_top_level_doc_is_reachable():
    """A document in docs/ that search can find but read_doc cannot open would
    be the worst failure here: the model cites it and gets an error."""
    for name in docs.list_docs()["documents"]:
        assert "content" in docs.read_doc(name), f"{name} is listed but unreadable"


# ---- search ----------------------------------------------------------------

def test_search_finds_a_document_by_its_name():
    matches = docs.search_docs("limits")["matches"]
    assert "limits" in [m["name"] for m in matches]


def test_search_finds_a_decision_written_in_a_body():
    """The reason this tool reads whole bodies rather than names and summaries.
    A question about how Wren works is answered in the middle of a document, not
    in its title — the same finding that made search_wiki read full text."""
    matches = docs.search_docs("thinking tokens empty response")["matches"]
    assert matches, "a phrase that only appears in document bodies found nothing"
    assert any("context" in m for m in matches), \
        "a body match came back with no quote, so nothing explains why it matched"


def test_an_empty_query_is_refused():
    assert "error" in docs.search_docs("   ")


def test_a_query_matching_nothing_returns_no_matches():
    """It must come back empty rather than erroring. The description tells the
    model to say the documentation does not cover it, and that instruction needs
    an empty result to act on."""
    assert docs.search_docs("zzzznotawordanywhere")["matches"] == []


def test_body_terms_match_on_word_boundaries(monkeypatch):
    """A body is prose, where substring matching is actively wrong: it is what
    would rank a document for the 'we' inside 'power'. The corpus is swapped
    for two crafted rows so the assertion is about the matcher and not about
    whatever words today's real prose happens to contain."""
    monkeypatch.setattr(docs, "_doc_texts", lambda: [
        {"name": "alpha", "summary": "about one thing", "body": "The power law applies."},
        {"name": "beta", "summary": "about another", "body": "We measured it."},
    ])
    assert [m["name"] for m in docs._search_docs("we")] == ["beta"], \
        "'we' matched inside 'power', so bodies are being matched as substrings"


def test_name_and_summary_terms_match_as_substrings(monkeypatch):
    """The other half of the same rule, and it must NOT be word-bounded: names
    are slugs, so a term has to be able to match inside 'module-map'."""
    monkeypatch.setattr(docs, "_doc_texts", lambda: [
        {"name": "module-map", "summary": "what lives where", "body": "nothing relevant"},
    ])
    assert docs._search_docs("modul")[0]["name"] == "module-map"


def test_a_term_scores_once_however_often_it_appears():
    """Otherwise a 30KB document outranks a short, precise one on repetition
    alone — and this corpus has a 96KB README in it."""
    matches = docs.search_docs("timezone")["matches"]
    assert matches
    assert matches[0]["name"] == "timezones", \
        "the document NAMED for the topic should outrank the ones that mention it"


def test_search_results_stay_under_the_flat_tool_result_cap():
    """search_docs has no TOOL_RESULT_CHAR_CAPS entry, which is only correct
    while its own caps keep it under agent/loop.py's flat 8000. A broad query
    matching most of the corpus is the worst case."""
    result = docs.search_docs("the a wren is to for and")
    assert len(json.dumps(result)) < 8000


def test_the_cap_says_so_rather_than_dropping_matches_silently():
    """A truncated result reads as complete to the model, which then reports
    that the documentation has nothing else on the topic."""
    result = docs.search_docs("wren")
    if len(result["matches"]) < len(docs._search_docs("wren")):
        assert "truncated" in result


# ---- reading ---------------------------------------------------------------

def test_a_short_document_comes_back_whole():
    result = docs.read_doc("reboot-recovery")
    assert "[Only the first" not in result["content"]


def test_a_long_document_is_trimmed_and_says_what_it_cut():
    """Thirteen of the 40 are over the budget and README.md is 96KB. Handed a
    silently truncated document the model treats what it got as the whole thing
    and reports that the docs do not cover something sitting in the part it
    never saw — the exact false negative the wiki's reader was built to stop."""
    content = docs.read_doc("limits")["content"]
    assert "[Only the first" in content
    assert "Not shown in full:" in content
    assert "Do NOT say this document" in content


def test_the_trim_notice_names_headings_that_can_be_read_back():
    """The notice is only useful if the names in it work as `section` arguments.
    A notice naming something read_doc then rejects is a dead end, and the model
    has nowhere else to go."""
    content = docs.read_doc("limits")["content"]
    named = content.split("Not shown in full: ")[1].split(". Call read_doc")[0]
    first = named.split(", ")[0]
    result = docs.read_doc("limits", section=first)
    assert "content" in result, f"the notice named {first!r} but reading it failed"
    assert result["section"]


def test_a_section_read_returns_only_that_section():
    result = docs.read_doc("limits", section="Why the limits exist at all")
    assert result["section"] == "Why the limits exist at all"
    assert result["content"].startswith("## Why the limits exist at all")


def test_section_matching_forgives_an_imperfect_heading():
    """The model is re-typing a heading it read in a trim notice, and these are
    long English phrases. An exact-match lookup would turn a dropped word into a
    dead end."""
    result = docs.read_doc("limits", section="why the limits exist")
    assert result.get("section") == "Why the limits exist at all"


def test_a_missing_section_names_the_ones_that_exist():
    """Say what exists rather than only saying no, so the retry is informed."""
    result = docs.read_doc("limits", section="something that is not a heading here")
    assert "error" in result
    assert result["sections"], "a refusal with no alternatives is a dead end"


def test_a_stopword_alone_does_not_match_a_section():
    """The bug this test was written by. The overlap branch drops words under 4
    chars as a stand-in for "not a stopword", and the proxy leaks — 'that' and
    'here' are both 4 letters. Asking for a section that does not exist
    returned "Limits that are not about the model" on the shared 'that'.

    A wrong section is worse than no section. No section returns the real
    heading list and the model retries; a wrong one it reads, answers from, and
    neither of them ever learns it got the wrong part of the document."""
    assert docs._match_section(
        [("Limits that are not about the model", "body")],
        "something that is not a heading here",
    ) is None


def test_the_stopword_guard_bites():
    """Proof the test above is not green for the wrong reason. Empty the
    stopword set — which is what a careless "simplify" would do — and the same
    call must match on 'that' again."""
    real = docs._STOPWORDS
    try:
        docs._STOPWORDS = frozenset()
        assert docs._match_section(
            [("Limits that are not about the model", "body")],
            "something that is not a heading here",
        ) is not None, "the unguarded version did not mismatch — this proves nothing"
    finally:
        docs._STOPWORDS = real


def test_a_single_real_word_still_matches_a_section():
    """The guard must not turn into "two words or nothing". One CONTENT word is
    a real match — the model asking about 'caps' means the caps section."""
    hit = docs._match_section(
        [("Overview", "a"), ("Tool-result caps", "b")],
        "how do the caps work",
    )
    assert hit is not None and hit[0] == "Tool-result caps"


def test_an_unknown_document_names_the_ones_that_exist():
    result = docs.read_doc("not-a-real-document")
    assert "error" in result
    assert "limits" in result["documents"]


def test_a_name_with_the_extension_still_resolves():
    """The model re-types names it read; '.md' is the likeliest thing it adds."""
    assert docs.read_doc("limits.md")["document"] == "limits"


def test_an_empty_name_is_refused():
    assert "error" in docs.read_doc("  ")


def test_a_traversal_attempt_finds_nothing():
    """The name comes from the model. Nothing here joins it onto a path — the
    corpus map is built first and a name that is not a key simply does not
    resolve — so there is no traversal to defend against. This asserts that
    property rather than a rejection message."""
    for attempt in ("../AGENTS.md", "../../etc/passwd", "docs/../AGENTS"):
        assert "error" in docs.read_doc(attempt)


def test_every_read_stays_under_its_backstop():
    """read_doc trims to MAX_DOC_CHARS and agent/loop.py backstops it at 14000.
    Keep the gap: the backstop counts the JSON-escaped result, and if it ever
    fires it re-cuts a document _fit_doc trimmed on purpose — taking the trim
    notice with it."""
    from agent.loop import TOOL_RESULT_CHAR_CAPS
    backstop = TOOL_RESULT_CHAR_CAPS["read_doc"]
    assert docs.MAX_DOC_CHARS < backstop
    for name in docs.list_docs()["documents"]:
        assert len(json.dumps(docs.read_doc(name))) < backstop, \
            f"reading {name} would be re-cut by the blind backstop"


def test_a_document_with_no_sections_says_so():
    """Rather than returning an empty result the model reads as "nothing here"."""
    assert docs._split_sections("# Title\n\nJust prose, no headings.\n") == []


# ---- summaries -------------------------------------------------------------

def test_every_document_has_a_summary():
    """The summary is what the model picks a read target from. A row with an
    empty one is a row it cannot act on."""
    for row in docs._doc_texts():
        assert row["summary"], f"{row['name']} produced no summary"


def test_the_summary_is_the_opening_paragraph_not_the_title():
    """The H1 restates the filename, which is already scored and scored higher."""
    summary = [r for r in docs._doc_texts() if r["name"] == "limits"][0]["summary"]
    assert summary.startswith("Wren runs on a small local model")


def test_a_document_opening_with_a_code_fence_still_summarises_as_prose():
    text = "# Thing\n\n```bash\nrun --this\n```\n\nWhat the thing actually is.\n"
    assert docs._summary(text) == "What the thing actually is."


def test_a_long_opening_paragraph_is_cut_on_a_word_boundary():
    text = "# Thing\n\n" + ("word " * 200)
    summary = docs._summary(text)
    assert len(summary) <= docs._SUMMARY_CHARS + 2
    assert summary.endswith("…")


# ---- the descriptions are the whole design ---------------------------------

def test_the_search_description_denies_pretraining():
    """AGENTS.md's catalogue rule. This corpus is the worst case for it: its
    topics (agents, scheduling, context limits) are exactly what a model can
    invent authoritative-sounding prose about, and an invented answer about how
    Wren works reads exactly like a real one."""
    description = docs.SEARCH_DOCS_SCHEMA["function"]["description"]
    assert "you do not know its contents" in description.lower()
    assert "only the documents this tool returns exist" in description.lower()


def test_the_search_description_says_what_to_do_on_no_matches():
    """The third clause of the catalogue rule, and the one most often dropped.
    Without it an empty result is filled in from pretraining."""
    assert "do not answer from your own knowledge" in \
        docs.SEARCH_DOCS_SCHEMA["function"]["description"].lower()


def test_the_read_description_points_at_the_section_argument():
    """Nine documents come back trimmed. If the model does not know the section
    argument exists, the trim notice is a dead end."""
    assert "section" in docs.READ_DOC_SCHEMA["function"]["description"]
