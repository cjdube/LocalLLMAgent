"""Tests for agent.tools.wiki — read-only access to the learnings wiki vault.

WIKI_VAULT_PATH is read fresh in _vault() on every call, so pointing it at a
tmp_path via monkeypatch fully isolates these from the real vault.
"""

from agent.tools import wiki


def _build_vault(root):
    """Create a minimal vault: wiki/index.md + one page. The real vault also has
    a raw/ dir, but that's ObsidianWikiAgent's inbox — Wren never reads it."""
    (root / "wiki").mkdir()
    (root / "wiki" / "index.md").write_text("# Index\n- [[speakers-bureau]]")
    (root / "wiki" / "log.md").write_text("log")
    (root / "wiki" / "speakers-bureau.md").write_text("Speakers Bureau page")


def test_read_index_and_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)

    assert wiki.read_wiki_index()["content"].startswith("# Index")
    # index.md and log.md are excluded from the page list.
    assert wiki.list_wiki_pages() == {"pages": ["speakers-bureau.md"]}
    assert wiki.read_wiki_page("speakers-bureau")["content"] == "Speakers Bureau page"
    # .md suffix is optional.
    assert wiki.read_wiki_page("speakers-bureau.md")["content"] == "Speakers Bureau page"


def test_missing_vault_errors(tmp_path, monkeypatch):
    missing = tmp_path / "missing-vault"
    monkeypatch.setenv("WIKI_VAULT_PATH", str(missing))

    for result in (wiki.read_wiki_index(), wiki.list_wiki_pages(),
                   wiki.read_wiki_page("x"), wiki.page_summaries(),
                   wiki.search_wiki("x")):
        assert "error" in result and "not found" in result["error"]
    assert not missing.exists()  # errored instead of creating a stray vault


def test_page_summaries_extracts_the_summary_line(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    (tmp_path / "wiki" / "lm-studio.md").write_text(
        "# LM Studio\n\n**Summary**: A local LLM execution platform.\n\nBody text.")

    pages = {p["name"]: p["summary"] for p in wiki.page_summaries()["pages"]}
    assert pages["lm-studio"] == "A local LLM execution platform."
    # A page without one still appears — daily_synthesis falls back to the name.
    assert pages["speakers-bureau"] == ""


# --- search ------------------------------------------------------------------
# The entry point. It replaced read_wiki_index and list_wiki_pages, both of which
# outgrew the 8000-char tool-result cap and handed the model a silent prefix.

def _add_page(root, name, summary):
    (root / "wiki" / f"{name}.md").write_text(
        f"# {name}\n\n**Summary**: {summary}\n\nBody.")


def test_search_matches_a_summary_the_filename_never_mentions(tmp_path, monkeypatch):
    # The whole reason search beats a filename list: the page for "how he prices
    # engagements" is named for the role, not for pricing.
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    _add_page(tmp_path, "fractional-product-leadership",
              "How he scopes and prices retainer engagements.")

    names = [m["name"] for m in wiki.search_wiki("pricing retainer")["matches"]]
    assert names == ["fractional-product-leadership"]


def test_search_matches_inside_a_slug(tmp_path, monkeypatch):
    # Page names are hyphenated slugs, so a bare term has to match inside one.
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    _add_page(tmp_path, "local-ai-compute-infrastructure", "Running models on the mini.")

    names = [m["name"] for m in wiki.search_wiki("compute")["matches"]]
    assert names == ["local-ai-compute-infrastructure"]


def test_search_ranks_a_name_hit_above_a_summary_hit(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    _add_page(tmp_path, "duckdb-analytics", "A columnar engine.")
    _add_page(tmp_path, "grocery-lists", "Built on duckdb for the analytics.")

    names = [m["name"] for m in wiki.search_wiki("duckdb")["matches"]]
    assert names == ["duckdb-analytics", "grocery-lists"]


def test_search_returns_summaries_with_the_names(tmp_path, monkeypatch):
    # read_wiki_page needs a name; the model needs the summary to pick which one.
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    _add_page(tmp_path, "lm-studio", "A local LLM execution platform.")

    assert wiki.search_wiki("lm-studio")["matches"] == [
        {"name": "lm-studio", "summary": "A local LLM execution platform."}]


def test_search_with_no_match_returns_empty_not_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    _add_page(tmp_path, "lm-studio", "A local LLM execution platform.")

    result = wiki.search_wiki("sourdough")
    assert result == {"matches": []}  # and no "truncated" key


def test_search_result_stays_under_the_tool_result_cap(tmp_path, monkeypatch):
    # The failure being fixed: a result the loop trims to a prefix the model
    # can't tell is a prefix. A broad query against a big vault must not do that.
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    for i in range(200):
        _add_page(tmp_path, f"project-note-{i:03d}", "A project page. " * 12)

    result = wiki.search_wiki("project")
    assert len(result["matches"]) <= wiki.MAX_SEARCH_RESULTS
    assert len(str(result)) < 8000  # OLLAMA_MAX_TOOL_RESULT_CHARS
    # Dropped matches are reported, not silent — the model can narrow instead of
    # assuming it saw the whole vault.
    assert "200" in result["truncated"]


def test_search_reports_nothing_truncated_when_everything_fits(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    _add_page(tmp_path, "lm-studio", "A local LLM execution platform.")

    assert "truncated" not in wiki.search_wiki("local")


def test_search_degrades_on_a_missing_vault(tmp_path, monkeypatch):
    missing = tmp_path / "gone"
    monkeypatch.setenv("WIKI_VAULT_PATH", str(missing))

    result = wiki.search_wiki("anything")
    assert "error" in result and "not found" in result["error"]
    assert not missing.exists()


def test_search_rejects_an_empty_query(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)

    assert "error" in wiki.search_wiki("   ")
    # Punctuation with no usable term is a no-match, not an error.
    assert wiki.search_wiki("???") == {"matches": []}


def test_search_schema_denies_the_model_its_own_answer(tmp_path, monkeypatch):
    # CLAUDE.md's catalogue-tool rule: a "what exists?" description must say the
    # list is not in the model's head, or pretraining invents plausible pages.
    description = wiki.SEARCH_WIKI_SCHEMA["function"]["description"].lower()
    assert "you do not know" in description
    assert "only the pages this tool returns exist" in description
    assert "no matches" in description  # says what to do when it finds nothing


# --- oversized pages ---------------------------------------------------------
# 7 of the vault's 390 pages are over the old flat 8000-char tool-result cap, and
# they're the central ones. A blind cut takes the tail, and the tail is where the
# [[link]] block lives.

def _long_page(sections, body_chars=6000):
    parts = ["---\ntags: []\n---\n\n# Big Page\n",
             "**Summary**: A big page.",
             "**Sources**: " + ", ".join(f"Source-{i}.md" for i in range(16)),
             "**Last updated**: 2026-08-16\n"]
    for name in sections:
        parts.append(f"## {name}\n\n" + ("filler text. " * (body_chars // 13)))
    parts.append("## Related pages\n\n- [[alpha]]\n- [[beta]]\n- [[gamma]]")
    return "\n".join(parts)


def test_fit_page_keeps_the_link_block_a_blind_cut_would_take(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    (tmp_path / "wiki" / "agentos.md").write_text(
        _long_page(["Overview", "Deployment", "Failure Modes"]))

    content = wiki.read_wiki_page("agentos")["content"]
    assert len(content) <= wiki.MAX_PAGE_CHARS
    # The whole link footer survives — it's the wiki's navigation.
    assert "## Related pages" in content
    for link in ("[[alpha]]", "[[beta]]", "[[gamma]]"):
        assert link in content


def test_fit_page_names_the_sections_it_dropped(tmp_path, monkeypatch):
    # Asked how AgentOS relates to SVPG, Wren read a silently cut agentos.md and
    # answered that SVPG was "not explicitly in the wiki" — from the 58% of a
    # page she never saw. The notice exists to make that answer impossible.
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    (tmp_path / "wiki" / "agentos.md").write_text(
        _long_page(["Overview", "Deployment", "Product Theory and SVPG Alignment"]))

    content = wiki.read_wiki_page("agentos")["content"]
    # Named as unread even though the cut left its heading visible — testing the
    # heading alone would call a mostly-missing section "read".
    assert "Product Theory and SVPG Alignment" in content
    assert "Not shown in full" in content
    assert "Do NOT say this page or the wiki lacks something" in content


def test_the_sources_line_is_dropped_from_every_page(tmp_path, monkeypatch):
    # Ingest provenance — 16 filenames, ~500 chars on the big pages, and never
    # the answer to a question. Dropped whether or not the page needs trimming.
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    (tmp_path / "wiki" / "small.md").write_text(
        "# Small\n\n**Summary**: Tiny.\n**Sources**: A.md, B.md\n\nBody text.")

    content = wiki.read_wiki_page("small")["content"]
    assert "**Sources**" not in content
    assert "**Summary**: Tiny." in content and "Body text." in content


def test_a_page_that_fits_is_returned_whole(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)

    content = wiki.read_wiki_page("speakers-bureau")["content"]
    assert content == "Speakers Bureau page"  # no notice, no trim
    assert "not shown" not in content


def test_fit_page_handles_a_long_page_with_no_headings(tmp_path, monkeypatch):
    # local-llm-agent.md is 9.8KB with zero H2s, which is why section-based
    # reading was rejected: there are no sections to name or fetch.
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    (tmp_path / "wiki" / "flat.md").write_text("# Flat\n\n" + ("prose. " * 4000))

    content = wiki.read_wiki_page("flat")["content"]
    assert len(content) <= wiki.MAX_PAGE_CHARS
    assert "characters of this page are shown" in content


def test_fit_page_survives_a_page_with_no_link_block(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    (tmp_path / "wiki" / "orphaned.md").write_text(
        "# Orphan\n\n## Only Section\n\n" + ("prose. " * 4000))

    content = wiki.read_wiki_page("orphaned")["content"]
    assert len(content) <= wiki.MAX_PAGE_CHARS
    assert "## Related pages" not in content  # nothing invented


# --- reading one section -----------------------------------------------------
# The escape hatch for the pages that still don't fit whole. The trim notice
# names the sections it cut; this is how Wren goes and reads one.

def test_a_named_section_comes_back_whole(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    (tmp_path / "wiki" / "agentos.md").write_text(
        _long_page(["Overview", "Deployment", "Product Theory and SVPG Alignment"]))

    result = wiki.read_wiki_page("agentos", "Product Theory and SVPG Alignment")
    assert result["section"] == "Product Theory and SVPG Alignment"
    assert result["content"].startswith("## Product Theory and SVPG Alignment")
    assert "characters of this page are shown" not in result["content"]  # whole
    assert "## Overview" not in result["content"]  # just the one section


def test_section_matching_forgives_a_reworded_heading(tmp_path, monkeypatch):
    # The model is retyping a 33-char heading it read in a notice. An exact-match
    # lookup would turn a dropped word into a dead end.
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    (tmp_path / "wiki" / "agentos.md").write_text(
        _long_page(["Overview", "Product Theory and SVPG Alignment"]))

    for asked in ("product theory and svpg alignment", "SVPG Alignment", "svpg"):
        assert wiki.read_wiki_page("agentos", asked)["section"] == \
            "Product Theory and SVPG Alignment"


def test_the_more_specific_heading_wins_over_a_shorter_one(tmp_path, monkeypatch):
    # "Overview" is a substring of half the phrasings a model might type, and it
    # comes first on the page — page order would hand it back every time.
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    (tmp_path / "wiki" / "agentos.md").write_text(
        _long_page(["Overview", "Deployment Overview and Rollout"]))

    result = wiki.read_wiki_page("agentos", "Deployment Overview and Rollout")
    assert result["section"] == "Deployment Overview and Rollout"


def test_one_shared_stopword_is_not_a_match(tmp_path, monkeypatch):
    # Headings here are English phrases; a shared "and" must not pass for one.
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    (tmp_path / "wiki" / "agentos.md").write_text(
        _long_page(["Configuration and Preference Management"]))

    assert "error" in wiki.read_wiki_page("agentos", "Revenue and Pricing")


def test_an_unmatched_section_names_what_the_page_does_have(tmp_path, monkeypatch):
    # Same rule as the catalogue tools: say what exists, don't just say no.
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    (tmp_path / "wiki" / "agentos.md").write_text(_long_page(["Overview", "Deployment"]))

    result = wiki.read_wiki_page("agentos", "quarterly revenue")
    assert "error" in result
    assert result["sections"] == ["Overview", "Deployment", "Related pages"]


def test_a_section_ask_on_a_page_with_no_headings_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    (tmp_path / "wiki" / "flat.md").write_text("# Flat\n\nJust prose, no headings.")

    result = wiki.read_wiki_page("flat", "Overview")
    assert "error" in result and "no sections" in result["error"]


def test_an_empty_section_argument_reads_the_whole_page(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)

    assert wiki.read_wiki_page("speakers-bureau", "")["content"] == "Speakers Bureau page"
    assert wiki.read_wiki_page("speakers-bureau", None)["content"] == "Speakers Bureau page"


def test_the_trim_notice_points_at_the_section_argument(tmp_path, monkeypatch):
    # Without this the model is told what it missed and given no way to get it.
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    (tmp_path / "wiki" / "agentos.md").write_text(
        _long_page(["Overview", "Deployment", "Product Theory and SVPG Alignment"]))

    content = wiki.read_wiki_page("agentos")["content"]
    assert "section argument" in content


def test_missing_page(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)

    assert "error" in wiki.read_wiki_page("does-not-exist")


def test_page_traversal_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    # A secret outside the wiki dir the model must not be able to reach via '../'.
    (tmp_path / "secret.md").write_text("secret")

    result = wiki.read_wiki_page("../secret")
    assert "error" in result and "outside" in result["error"]


def test_dotfiles_skipped_in_listings(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    # macOS AppleDouble sidecars and the ingest bookkeeping file must not show up.
    (tmp_path / "wiki" / "._speakers-bureau.md").write_text("junk")
    (tmp_path / "wiki" / ".ingested.json").write_text("[]")

    assert wiki.list_wiki_pages() == {"pages": ["speakers-bureau.md"]}


def test_empty_name_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)

    assert "error" in wiki.read_wiki_page("   ")


# --- lens discovery ---------------------------------------------------------

def _add_lens(root, name, description="my standards", body="Ship small."):
    (root / "wiki" / f"{name}.md").write_text(
        f"---\nlens: true\ndescription: {description}\n---\n\n{body}"
    )


def test_list_lenses_only_returns_marked_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)  # speakers-bureau has no frontmatter — not a lens
    _add_lens(tmp_path, "product-principles", description="product standards")

    lenses = wiki.list_lenses()["lenses"]
    assert lenses == [{"name": "product-principles", "description": "product standards"}]


def test_plain_frontmatter_without_lens_marker_is_not_a_lens(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    # A page with a description but no `lens: true` must not be picked up.
    (tmp_path / "wiki" / "agent-safety.md").write_text(
        "---\ndescription: notes on agent safety\n---\n\nbody"
    )
    assert wiki.list_lenses()["lenses"] == []


def test_render_lenses_index_lists_names_and_descriptions(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    _add_lens(tmp_path, "product-principles", description="product standards")

    block = wiki.render_lenses_index()
    assert "evaluate_against" in block
    assert "- product-principles: product standards" in block


def test_render_lenses_index_empty_without_lenses(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    assert wiki.render_lenses_index() == ""


def test_list_lenses_degrades_on_missing_vault(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path / "missing"))
    # No vault → no lenses, and the prompt-build render must not raise.
    assert wiki.list_lenses() == {"lenses": []}
    assert wiki.render_lenses_index() == ""


# --- truncation is logged, not silent ---------------------------------------

class _FakeLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, msg):
        self.warnings.append(msg)


def test_char_budget_truncation_warns_with_counts_and_names(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    # Three lenses whose descriptions each eat ~half the budget: the char cap
    # bites well before MAX_INDEX_LENSES, which is the case that bit in practice.
    for name in ("aaa-lens", "bbb-lens", "ccc-lens"):
        _add_lens(tmp_path, name, description="x" * 300)

    log = _FakeLogger()
    block = wiki.render_lenses_index(log)

    # Only the first fits; the other two are dropped from the prompt entirely.
    assert "aaa-lens" in block and "bbb-lens" not in block and "ccc-lens" not in block
    assert len(log.warnings) == 1
    warning = log.warnings[0]
    assert "1 of 3 lenses" in warning
    assert "bbb-lens" in warning and "ccc-lens" in warning
    assert str(wiki.MAX_INDEX_CHARS) in warning


def test_lens_cap_truncation_names_the_count_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    # Descriptions short enough that the budget never binds — only the count does.
    for i in range(wiki.MAX_INDEX_LENSES + 2):
        _add_lens(tmp_path, f"lens-{i:02d}", description="short")

    log = _FakeLogger()
    wiki.render_lenses_index(log)

    assert len(log.warnings) == 1
    warning = log.warnings[0]
    assert f"{wiki.MAX_INDEX_LENSES}-lens cap" in warning
    assert f"{wiki.MAX_INDEX_LENSES} of {wiki.MAX_INDEX_LENSES + 2} lenses" in warning


def test_no_warning_when_every_lens_fits(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    _add_lens(tmp_path, "product-principles", description="product standards")

    log = _FakeLogger()
    assert "product-principles" in wiki.render_lenses_index(log)
    assert log.warnings == []


def test_truncation_without_a_logger_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    for name in ("aaa-lens", "bbb-lens"):
        _add_lens(tmp_path, name, description="x" * 300)

    # The prompt build must never break on a logger-less call (the CLI path).
    assert "aaa-lens" in wiki.render_lenses_index()


# --- project pages ----------------------------------------------------------
# The marker exists because page names and directory names routinely disagree:
# the page for the LocalLLMAgent checkout is `wren`, which no slug rule reaches.

def _add_project(root, name, repo="cjdube/Thing", path="Thing",
                 summary="A thing he built."):
    (root / "wiki" / f"{name}.md").write_text(
        f"---\nproject: true\nrepo: {repo}\npath: {path}\n---\n\n"
        f"# {name}\n\n**Summary**: {summary}\n\nWhy it was built this way."
    )


def test_list_project_pages_only_returns_marked_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)  # speakers-bureau has no frontmatter — not a project
    _add_project(tmp_path, "wren", repo="cjdube/LocalLLMAgent", path="LocalLLMAgent")

    assert wiki.list_project_pages()["projects"] == [
        {"name": "wren", "repo": "cjdube/LocalLLMAgent", "path": "LocalLLMAgent",
         "summary": "A thing he built."}
    ]


def test_a_lens_is_not_a_project(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    _add_lens(tmp_path, "product-principles")

    assert wiki.list_project_pages()["projects"] == []
    assert [lens["name"] for lens in wiki.list_lenses()["lenses"]] == ["product-principles"]


def test_project_page_without_a_path_is_still_listed(tmp_path, monkeypatch):
    # It just can't be joined to a checkout — the caller reports that rather
    # than guessing which directory was meant.
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    (tmp_path / "wiki" / "orphan.md").write_text(
        "---\nproject: true\n---\n\n# Orphan\n\n**Summary**: No path given.")

    assert wiki.list_project_pages()["projects"] == [
        {"name": "orphan", "repo": "", "path": "", "summary": "No path given."}]


def test_list_project_pages_degrades_to_empty_without_a_vault(tmp_path, monkeypatch):
    # The merge is optional; a misconfigured vault must cost it, not the run.
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path / "gone"))
    assert wiki.list_project_pages() == {"projects": []}


def test_an_empty_repo_does_not_capture_the_next_line(tmp_path, monkeypatch):
    # `\s*` around an empty value eats the newline and swallows whatever follows.
    # On the real vault this made screenwatch's repo come back as
    # "path: screenwatch-kit", which would have silently mis-joined the page.
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    _add_project(tmp_path, "screenwatch", repo="", path="screenwatch-kit")

    project = wiki.list_project_pages()["projects"][0]
    assert project["repo"] == ""
    assert project["path"] == "screenwatch-kit"


def test_an_empty_description_does_not_capture_the_next_line(tmp_path, monkeypatch):
    # Same `\s`-crosses-the-newline bug as the empty-repo case above, on the
    # older lens regex. A blank description made the FOLLOWING frontmatter key
    # the lens's description — and render_lenses_index puts that straight into
    # the chat system prompt, spending its char budget on "tone: blunt".
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path))
    _build_vault(tmp_path)
    (tmp_path / "wiki" / "product-principles.md").write_text(
        "---\nlens: true\ndescription:\ntone: blunt\n---\n\n# Product Principles")

    assert wiki.list_lenses()["lenses"] == [
        {"name": "product-principles", "description": ""}]
    # A lens with no usable description still renders — as its bare name.
    assert "- product-principles\n" in wiki.render_lenses_index() + "\n"
