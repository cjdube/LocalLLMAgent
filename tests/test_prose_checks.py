"""Tests for agent/tools/prose_checks.py — the deterministic checks a lens opts
into via frontmatter. Pure string work: no vault, no model, no network.

The behaviour these pin down came from measured model failures on the ai-slop
lens (see the module docstring): the counts have to be exact, and a check that
found nothing has to say so out loud, because "none" is what stops the model
inventing a finding of that kind."""

import pytest

from agent.tools import prose_checks as pc


# --------------------------------------------------------------------------- #
# checks_config — frontmatter opt-in
# --------------------------------------------------------------------------- #

def test_no_frontmatter_means_no_checks():
    assert pc.checks_config("Just a body, no frontmatter.") == {}


def test_lens_without_the_keys_opts_out():
    text = "---\nlens: true\ndescription: standards\n---\n\nBody."
    assert pc.checks_config(text) == {}


def test_both_keys_parse():
    text = ("---\nlens: true\nmax_em_dashes_per_sentence: 1\n"
            "banned_phrases: paradigm shift, cutting-edge\n---\n\nBody.")
    assert pc.checks_config(text) == {
        "max_em_dashes_per_sentence": 1,
        "banned_phrases": ["paradigm shift", "cutting-edge"],
    }


def test_either_key_alone_is_enough():
    only_dashes = "---\nmax_em_dashes_per_sentence: 2\n---\n"
    only_phrases = "---\nbanned_phrases: game changer\n---\n"
    assert pc.checks_config(only_dashes) == {"max_em_dashes_per_sentence": 2}
    assert pc.checks_config(only_phrases) == {"banned_phrases": ["game changer"]}


def test_a_zero_threshold_is_kept_not_treated_as_absent():
    # 0 is a legitimate setting (ban em dashes outright) and must survive the
    # falsy-value trap.
    assert pc.checks_config("---\nmax_em_dashes_per_sentence: 0\n---\n") == {
        "max_em_dashes_per_sentence": 0}


def test_a_malformed_threshold_is_ignored_not_raised():
    # A typo in the vault must not break an evaluation (degrade, don't crash).
    assert pc.checks_config("---\nmax_em_dashes_per_sentence: one\n---\n") == {}


def test_blank_and_stray_commas_drop_out_of_the_phrase_list():
    text = "---\nbanned_phrases: game changer, , ,cutting-edge,\n---\n"
    assert pc.checks_config(text) == {"banned_phrases": ["game changer", "cutting-edge"]}


# --------------------------------------------------------------------------- #
# em_dash_sentences — the count that replaced a model judgement
# --------------------------------------------------------------------------- #

def test_one_em_dash_per_sentence_is_not_a_finding():
    # The measured false positive: the model called a comma-heavy sentence an
    # em-dash cluster. One dash per sentence is the user's normal voice.
    text = ("Drop it — don't substitute a gray-area source. "
            "Convert before comparing — the windows are local.")
    assert pc.em_dash_sentences(text, 1) == []


def test_two_in_one_sentence_is_a_finding():
    text = "I built it — a robust setup — and it works. A clean one — just one."
    assert pc.em_dash_sentences(text, 1) == [
        "I built it — a robust setup — and it works."]


def test_newlines_end_a_sentence_so_bullets_dont_merge():
    # Two bullets with one dash each must not be read as one two-dash sentence.
    text = "- first — one dash\n- second — one dash"
    assert pc.em_dash_sentences(text, 1) == []


def test_findings_come_back_in_document_order():
    text = "A — b — c. Filler. D — e — f."
    assert pc.em_dash_sentences(text, 1) == ["A — b — c.", "D — e — f."]


def test_a_very_long_sentence_is_truncated_for_quoting():
    text = "x " * 400 + "— a — b."
    hit = pc.em_dash_sentences(text, 1)[0]
    assert len(hit) == pc._MAX_QUOTE_CHARS


# --------------------------------------------------------------------------- #
# banned_phrases_found — exact matching, case-insensitive
# --------------------------------------------------------------------------- #

def test_phrases_match_case_insensitively_and_keep_declared_order():
    text = "The Cutting-Edge platform is a PARADIGM SHIFT."
    assert pc.banned_phrases_found(text, ["paradigm shift", "cutting-edge"]) == [
        "paradigm shift", "cutting-edge"]


def test_a_phrase_repeated_is_reported_once():
    text = "paradigm shift here, paradigm shift there"
    assert pc.banned_phrases_found(text, ["paradigm shift"]) == ["paradigm shift"]


def test_absent_phrases_are_not_reported():
    assert pc.banned_phrases_found("clean prose", ["paradigm shift"]) == []


def test_a_curly_apostrophe_in_the_target_still_matches_an_ascii_phrase():
    # A lens is typed with ASCII apostrophes; a fetched page renders them as
    # U+2019. Exact substring matching missed every apostrophe phrase, reported
    # "none", and looked identical to a clean draft.
    text = "It’s worth noting that the reality is simple."
    assert pc.banned_phrases_found(text, ["it's worth noting"]) == ["it's worth noting"]


def test_a_curly_apostrophe_in_the_lens_matches_an_ascii_target():
    # The mirror case — the lens page is edited in something that smart-quotes.
    text = "it's worth noting the build time"
    assert pc.banned_phrases_found(text, ["it’s worth noting"]) == ["it’s worth noting"]


def test_curly_double_quotes_fold_too():
    assert pc.banned_phrases_found("he called it “best in class” today",
                                   ['"best in class"']) == ['"best in class"']


# --------------------------------------------------------------------------- #
# render_checks_block — what the model actually receives
# --------------------------------------------------------------------------- #

def test_no_config_renders_nothing():
    assert pc.render_checks_block("anything — at all", {}) == ""


def test_a_passing_check_says_none_out_loud():
    # This is the anti-fabrication half: silence let the model invent an
    # em-dash finding, so a passed check states that it passed.
    block = pc.render_checks_block("one — dash only", {"max_em_dashes_per_sentence": 1})
    assert "none" in block
    assert "no em-dash finding to report" in block


def test_a_failing_check_quotes_the_offending_sentence():
    block = pc.render_checks_block("I built it — a setup — and it works.",
                                   {"max_em_dashes_per_sentence": 1})
    assert "1 found" in block
    assert "I built it — a setup — and it works." in block


def test_found_phrases_are_listed_and_absent_ones_reported_as_none():
    hit = pc.render_checks_block("a paradigm shift", {"banned_phrases": ["paradigm shift"]})
    assert '"paradigm shift"' in hit
    miss = pc.render_checks_block("clean", {"banned_phrases": ["paradigm shift"]})
    assert "none" in miss and "no banned-phrase finding to report" in miss
    assert '"paradigm shift"' not in miss


def test_the_block_tells_the_model_the_results_are_authoritative():
    block = pc.render_checks_block("x", {"banned_phrases": ["y"]})
    assert "authoritative" in block
    assert "do not re-derive" in block


def test_only_the_enabled_checks_appear():
    block = pc.render_checks_block("some text", {"banned_phrases": ["y"]})
    assert "em dash" not in block


def test_a_flood_of_hits_is_capped():
    text = "\n".join(f"line{i} — a — b." for i in range(50))
    block = pc.render_checks_block(text, {"max_em_dashes_per_sentence": 1})
    assert "50 found" in block                      # the count is honest...
    assert block.count('    - "') == pc._MAX_REPORTED   # ...the quoting is bounded
