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

The two SIBLING corpora are the opposite case and get the opposite treatment.
ObsidianWikiAgent and ScribeJay do NOT ship with this checkout and may not be
cloned at all, so an assertion against the real ones is a statement about the
developer's machine. tests/conftest.py pins both roots at paths that do not
exist — which makes the default here the missing-checkout degrade — and the
`sibling_repos` fixture below builds a crafted stand-in for the tests that need
one.
"""

import json

import pytest

from agent.tools import docs


# ---- the sibling corpora ---------------------------------------------------

@pytest.fixture
def sibling_repos(tmp_path, monkeypatch):
    """A crafted stand-in for both sibling checkouts: the collision, every
    exclusion, and nothing else.

    Crafted rather than copied. A test that needs the real repos cloned is a
    test that fails on a clean box, and the failures worth catching here are
    structural ("a sibling's AGENTS.md crept in", "the walk went recursive"),
    which invented files show just as well as real ones.
    """
    wiki = tmp_path / "wiki-repo"
    scribejay = tmp_path / "scribejay-repo"
    for root in (wiki, scribejay):
        (root / "docs" / "reviews").mkdir(parents=True)
        (root / "AGENTS.md").write_text("# AGENTS\n\nRun pytest before you commit.\n")
        (root / "CLAUDE.md").write_text("@AGENTS.md\n")
        (root / "docs" / "reviews" / "plan.md").write_text("# Audit\n\nAn audit plan.\n")

    (wiki / "README.md").write_text("# ObsidianWikiAgent\n\nWIKI_README_SENTINEL engine.\n")
    (wiki / "SECURITY.md").write_text("# Security\n\nWIKI_SECURITY_SENTINEL boundary.\n")
    (wiki / "docs" / "agent-context.md").write_text(
        "# Agent context\n\nWIKI_CONTEXT_SENTINEL ingest rationale.\n")
    (wiki / "tools" / "lint_defects" / "pages").mkdir(parents=True)
    (wiki / "tools" / "lint_defects" / "pages" / "broken.md").write_text(
        "# Broken page\n\nA deliberately defective fixture page.\n")

    (scribejay / "README.md").write_text(
        '<img src="assets/x.svg">\n\n# ScribeJay\n\nSJ_README_SENTINEL keeps the record.\n')
    (scribejay / "docs" / "timezones.md").write_text(
        "# Timezones\n\nSJ_TZ_SENTINEL every source stamps UTC.\n")
    (scribejay / "docs" / "architecture.md").write_text(
        "# Architecture\n\nSJ_ARCHITECTURE_SENTINEL how the record is written.\n")
    (scribejay / "scribejay").mkdir()
    (scribejay / "scribejay" / "persona.md").write_text(
        "# Persona\n\nYou are ScribeJay. Write in this voice.\n")

    monkeypatch.setenv("WREN_WIKI_REPO_PATH", str(wiki))
    monkeypatch.setenv("WREN_SCRIBEJAY_REPO_PATH", str(scribejay))
    return {"wiki": wiki, "scribejay": scribejay}


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


def test_a_prefix_shaped_traversal_attempt_finds_nothing(sibling_repos):
    """The repo prefix is the one thing in a name that LOOKS like a path, and
    the dropped-prefix fallback is the one new place a model string touches
    resolution. Neither joins anything onto a path — the fallback matches
    against keys already in the map — and this asserts that, including the
    '/passwd' case the suffix match is the closest to."""
    for attempt in ("scribejay/../../etc/passwd", "wiki/../AGENTS",
                    "../ScribeJay/AGENTS", "/etc/passwd", "/passwd",
                    "scribejay/../../ObsidianWikiAgent/AGENTS"):
        assert "error" in docs.read_doc(attempt), attempt


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


# ---- three corpora, one namespace ------------------------------------------

def test_a_colliding_sibling_document_is_reachable_and_is_the_right_file(sibling_repos):
    """The reason the repo prefix exists at all.

    EIGHT ScribeJay documents share a filename stem with one of Wren's —
    llm-backend, logs, model-constraints, ntfy-setup, opaque-identifiers,
    readme, timezones, usage-ledger — and ObsidianWikiAgent shares one (readme).
    _doc_paths() keys on the stem and uses setdefault, so a flat merge would
    drop every one of them silently, and Wren would report in good faith that
    ScribeJay has no timezone document while reading her own.
    """
    names = docs.list_docs()["documents"]
    assert "timezones" in names and "scribejay/timezones" in names

    assert "SJ_TZ_SENTINEL" in docs.read_doc("scribejay/timezones")["content"]
    wrens = docs.read_doc("timezones")
    assert wrens["document"] == "timezones"
    assert "SJ_TZ_SENTINEL" not in wrens["content"]


def test_wrens_own_document_wins_a_tie_with_a_siblings(sibling_repos):
    """The question was asked of Wren, so her copy leads. Without an explicit
    rule the ALPHABET decided it — the sort key was (-score, name) and
    's' < 't', so 'scribejay/timezones' beat 'timezones' on the real corpus."""
    top = docs.search_docs("timezones")["matches"][0]["name"]
    assert "/" not in top, f"a sibling's document led the results: {top}"


def test_the_own_first_tie_break_bites(sibling_repos, monkeypatch):
    """Prove the test above is not green for the wrong reason.

    Put the old two-part sort key back and the sibling must win. If this ever
    stops failing-then-passing, the test above has stopped proving anything —
    the same guard-on-the-guard as test_the_stopword_guard_bites.
    """
    monkeypatch.setattr(docs, "_rank_key", lambda score, name: (-score, name))
    assert docs.search_docs("timezones")["matches"][0]["name"] == "scribejay/timezones"


def test_a_missing_checkout_degrades_to_wrens_own_documents():
    """A sibling can be moved, unmounted, mid-upgrade or never cloned. That
    means "no ScribeJay documents today", not a failed tool call — so nothing
    raises, no prefixed name appears, and Wren's own corpus still answers.

    This is the conftest default, so it is also what every other test in this
    file runs against.
    """
    names = docs.list_docs()["documents"]
    assert names and not any("/" in name for name in names)
    assert "timezones" in [m["name"] for m in docs.search_docs("timezones")["matches"]]


def test_a_root_that_is_not_a_directory_degrades_the_same_way(tmp_path, monkeypatch):
    """is_dir() is the check, not exists(): a path pointing at a FILE, or at a
    directory with no docs/, must behave exactly like a missing one."""
    a_file = tmp_path / "not-a-repo.txt"
    a_file.write_text("x")
    bare = tmp_path / "bare-repo"
    bare.mkdir()
    monkeypatch.setenv("WREN_WIKI_REPO_PATH", str(a_file))
    monkeypatch.setenv("WREN_SCRIBEJAY_REPO_PATH", str(bare))
    assert not any("/" in name for name in docs.list_docs()["documents"])


def test_dropping_the_repo_prefix_still_resolves_a_unique_stem(sibling_repos):
    """The likeliest retype: the model reads 'scribejay/architecture' and types
    'architecture'. Resolved against keys already in the map, so it adds no
    traversal surface — and only when exactly one document ends in that stem."""
    result = docs.read_doc("architecture")
    assert result["document"] == "scribejay/architecture"
    assert "SJ_ARCHITECTURE_SENTINEL" in result["content"]


def test_a_bare_name_that_is_wrens_own_never_resolves_to_a_siblings(sibling_repos):
    """Bare means Wren's. A stem she owns matches exactly and never reaches the
    dropped-prefix fallback, so 'readme' cannot become 'scribejay/readme'."""
    assert docs.read_doc("readme")["document"] == "readme"


def test_an_ambiguous_bare_stem_names_both_candidates(monkeypatch):
    """When two siblings share a stem Wren does not own, the fallback must
    refuse and name both rather than pick one. The real corpus cannot currently
    produce this — OWA and ScribeJay share only 'readme', which Wren owns — so
    the condition is constructed."""
    monkeypatch.setattr(docs, "_doc_paths", lambda: {
        "wiki/logs": docs._REPO_ROOT / "README.md",
        "scribejay/logs": docs._REPO_ROOT / "README.md",
    })
    result = docs.read_doc("logs")
    assert "error" in result
    assert sorted(result["documents"]) == ["scribejay/logs", "wiki/logs"]


# ---- the sibling corpus boundaries -----------------------------------------

def test_a_siblings_agents_md_stays_out_of_the_corpus(sibling_repos):
    """Worse than Wren's own AGENTS.md exclusion, not merely the same: a
    sibling's is ANOTHER repo's maintenance contract. Wren reading "run pytest
    before you commit" as an instruction aimed at her is bad; reading one aimed
    at a repo she may not even touch is worse."""
    names = docs.list_docs()["documents"]
    assert "wiki/agents" not in names and "scribejay/agents" not in names


def test_a_pointer_claude_md_stays_out(sibling_repos):
    """All three repos ship a CLAUDE.md that is an import-only pointer. Root
    files are NAMED in _SIBLING_REPOS, never globbed — this is what notices if
    that ever becomes a glob."""
    names = docs.list_docs()["documents"]
    assert "wiki/claude" not in names and "scribejay/claude" not in names


def test_a_siblings_gitignored_reviews_dir_stays_out(sibling_repos):
    """Every one of the three repos gitignores docs/reviews/. iterdir() rather
    than rglob() is the whole mechanism, so a switch to a recursive walk would
    pull the audit plans of all three in at once."""
    assert "wiki/plan" not in docs.list_docs()["documents"]
    assert "scribejay/plan" not in docs.list_docs()["documents"]


def test_the_wiki_engines_lint_fixtures_stay_out(sibling_repos):
    """tools/lint_defects/pages/ holds deliberately FABRICATED defective wiki
    pages — fixtures for that repo's linter. Feeding them to Wren would put
    invented wiki claims in the same corpus she is told to trust, and she has
    no way to tell them from documentation."""
    assert "wiki/broken" not in docs.list_docs()["documents"]


def test_another_agents_persona_stays_out(sibling_repos):
    """scribejay/persona.md is ScribeJay's system-prompt material. Wren adopting
    another agent's voice because a search hit landed it in her context is a
    failure with no error message. It sits outside both docs/ and the root
    list, so the exclusion is by construction."""
    assert "scribejay/persona" not in docs.list_docs()["documents"]


def test_every_sibling_document_is_readable_and_summarised(sibling_repos):
    """The three sweeps the Wren-only tests already do, run over the siblings:
    everything listed opens, has a summary, and fits the backstop."""
    from agent.loop import TOOL_RESULT_CHAR_CAPS
    backstop = TOOL_RESULT_CHAR_CAPS["read_doc"]
    siblings = [n for n in docs.list_docs()["documents"] if "/" in n]
    assert siblings, "the fixture contributed no documents"
    for name in siblings:
        result = docs.read_doc(name)
        assert "content" in result, name
        assert len(json.dumps(result)) < backstop, name
    for row in docs._doc_texts():
        if "/" in row["name"]:
            assert row["summary"], row["name"]


def test_a_readme_opening_with_an_html_tag_summarises_as_prose():
    """ScribeJay's README opens with an <img> badge line ABOVE its H1, and
    _H1_RE strips the H1 from anywhere — so without '<' in the skip list the
    summary is the tag. Same shape as skipping a table row, not HTML
    stripping."""
    text = '<img src="x.svg" width="72">\n\n# Thing\n\nWhat the thing actually is.\n'
    assert docs._summary(text) == "What the thing actually is."


# ---- the descriptions carry the three-corpus design ------------------------

def test_the_search_description_names_all_three_systems(sibling_repos):
    """The model reaches this tool through a load_tools hop and will not make
    that hop for a question it does not recognise. "How do my notes get into the
    wiki" has to be findable in the words of the description itself."""
    description = docs.SEARCH_DOCS_SCHEMA["function"]["description"].lower()
    assert "wiki engine" in description
    assert "scribejay" in description
    assert "how his notes get into the wiki" in description
    # The catalogue clauses must survive the rewrite.
    assert "you do not know its contents" in description
    assert "only the documents this tool returns exist" in description


def test_the_read_description_explains_the_slash():
    """The prefix only works if the model passes it back. The description is the
    only place that can say what a slash means and that dropping it silently
    returns Wren's copy instead of the one that was asked for."""
    description = docs.READ_DOC_SCHEMA["function"]["description"]
    assert "slash" in description.lower()
    assert "scribejay/architecture" in description
    assert "exactly as the search returned it" in description.lower()
