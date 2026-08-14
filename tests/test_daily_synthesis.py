"""Tests for tasks/daily_synthesis.py — the pure matching/parsing helpers, and
main()'s branches: a genuine overlap pushes one nudge, no overlap or a NONE
model reply pushes nothing, and a dead source degrades instead of crashing.

Every external source (Chrome history, YouTube Likes, wiki, opportunities) and
the model/warm/notify calls are stubbed — no Chrome DB, no Google, no model, no
push. TIMEZONE is pinned so the prior-day window is deterministic, not the
host's zone (CLAUDE.md: UTC→local day windows)."""

import logging
from datetime import timedelta

import pytest

from tasks import daily_synthesis as ds

_LOG = logging.getLogger("test_daily_synthesis")

# SYNTHESIS_DIR (the vault's nudges/) is redirected to tmp_path suite-wide by
# tests/conftest.py::_isolate_learnings_dir, so persist_or_email writes there,
# never the real vault.


@pytest.fixture(autouse=True)
def _pin_timezone(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "America/New_York")


@pytest.fixture
def stub_sources(monkeypatch):
    """Default every source to empty and neutralize model/warm/push. Tests
    override individual sources to shape a scenario."""
    calls = {"pushes": [], "model": 0}

    # setup_logger builds a logger with propagate=False (so a task's output stays
    # in its own file), which caplog cannot see. Swap in a propagating one — the
    # WARNINGs on an unusable model reply are part of this task's contract, since
    # silence is its common case and only a WARNING reaches the log inspector.
    def _logger(task_name):
        logger = logging.getLogger(f"test_{task_name}")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        logger.propagate = True
        return logger
    monkeypatch.setattr(ds, "setup_logger", _logger)

    monkeypatch.setattr(ds, "fetch_chrome_history", lambda *a, **k: {"sites": []})
    monkeypatch.setattr(ds, "fetch_liked_videos", lambda *a, **k: {"videos": []})
    monkeypatch.setattr(ds, "page_summaries", lambda: {"pages": []})
    monkeypatch.setattr(ds, "get_watchlist", lambda: [])
    monkeypatch.setattr(ds, "list_opportunities", lambda **k: {"opportunities": []})
    monkeypatch.setattr(ds, "warm_model", lambda **k: None)

    def _model(**k):
        calls["model"] += 1
        return "- You dug into DuckDB — it fits your 'duckdb-analytics' note; want a summary?"
    monkeypatch.setattr(ds, "complete_text", _model)
    monkeypatch.setattr(ds, "notify",
                        lambda **k: calls["pushes"].append(k) or {"ok": True})
    return calls


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #

def test_tokenize_filters_short_and_stopwords():
    toks = ds._tokenize("DuckDB docs API from the guide")
    assert "duckdb" in toks
    assert "api" not in toks          # too short
    assert "docs" not in toks and "from" not in toks  # stopwords


def test_candidate_pairs_scores_and_caps():
    signals = [{"kind": "watched", "text": "DuckDB tips", "tokens": {"duckdb", "analytics"}}]
    anchors = [
        {"kind": "wiki page", "label": "duckdb-analytics", "tokens": {"duckdb", "analytics"}},
        {"kind": "wiki page", "label": "kubernetes", "tokens": {"kubernetes"}},
    ]
    pairs = ds.candidate_pairs(signals, anchors)
    assert len(pairs) == 1                       # only the overlapping anchor
    assert pairs[0]["anchor"]["label"] == "duckdb-analytics"
    assert pairs[0]["score"] == 1.0              # both tokens of a 2-token set
    assert pairs[0]["kind"] == "anchor"


# --------------------------------------------------------------------------- #
# _match — the size-normalized score, and the broad-vs-broad guard
# --------------------------------------------------------------------------- #

def _broad(prefix, *shared):
    """A token set past _BROAD_SET_SIZE whose filler is unique to `prefix`, so two
    _broad() sets overlap only in the tokens passed as `shared`."""
    return {f"{prefix}{i}" for i in range(ds._BROAD_SET_SIZE + 1)} | set(shared)


def test_match_normalizes_by_the_smaller_set():
    # A wordy signal sharing both tokens of a 2-token anchor beats one sharing 3 of
    # a dozen — ranking by raw count would put them the other way round.
    tight = ds._match({"duckdb", "analytics"} | _broad("a"), {"duckdb", "analytics"})
    loose = ds._match(_broad("b", "alpha", "beta", "gamma"),
                      _broad("c", "alpha", "beta", "gamma"))
    assert len(loose["overlap"]) > len(tight["overlap"])   # raw count says otherwise
    assert tight["score"] > loose["score"]


def test_match_rejects_single_token_between_two_broad_sets():
    assert ds._match(_broad("a", "kubernetes"), _broad("b", "kubernetes")) is None


def test_match_keeps_single_token_when_one_side_is_short():
    # The page `gemma-4` tokenizes to {"gemma"} — one token is all it has to offer,
    # so the broad-set rule must not swallow it.
    m = ds._match(_broad("a", "gemma"), {"gemma"})
    assert m is not None and m["score"] == 1.0


def test_match_without_overlap_is_none():
    assert ds._match({"terraform"}, {"cooking"}) is None


# --------------------------------------------------------------------------- #
# generic_tokens — the corpus-frequency filter that makes summary anchors usable
# --------------------------------------------------------------------------- #

def _anchor(label, *tokens):
    return {"kind": "wiki page", "label": label, "summary": "", "tokens": set(tokens)}


def test_generic_tokens_finds_the_vault_s_filler_words():
    # "agent" is in every summary here, "duckdb" in one.
    anchors = [_anchor(f"p{i}", "agent", f"topic{i}") for i in range(40)]
    anchors.append(_anchor("duckdb-analytics", "duckdb", "columnar"))

    generic = ds.generic_tokens(anchors)
    assert "agent" in generic
    assert "duckdb" not in generic and "columnar" not in generic


def test_generic_tokens_empty_corpus():
    assert ds.generic_tokens([]) == set()


def test_candidate_pairs_ignores_a_match_on_only_generic_tokens():
    # Four of five candidates were {agent, design} / {backend, local} coincidences
    # before this: words that are everywhere in the vault carry no signal.
    anchors = [_anchor(f"p{i}", "agent", "design", f"topic{i}") for i in range(40)]
    signal = _sig("chrome", "Some agent design post", {"agent", "design", "unrelated"})

    assert ds.candidate_pairs([signal], anchors) == []


def test_candidate_pairs_keeps_a_distinctive_token_match():
    # The omlx case: "I deleted LM Studio after a week with omlx" against the
    # lm-studio page. "studio" is rare in the corpus, so it still counts.
    anchors = [_anchor(f"p{i}", "agent", f"topic{i}") for i in range(40)]
    anchors.append(_anchor("lm-studio", "studio", "agent"))
    signal = _sig("chrome", "I deleted LM Studio after a week with omlx",
                  {"deleted", "studio", "week", "omlx"})

    pairs = ds.candidate_pairs([signal], anchors)
    assert len(pairs) == 1 and pairs[0]["anchor"]["label"] == "lm-studio"


def test_generic_filter_does_not_apply_to_echoes():
    # An echo's coincidence is temporal: the same word through two channels in one
    # day means something even when the word is common in the wiki.
    signals = [
        _sig("chrome", "Claude Design Tutorial", {"claude", "design", "tutorial"}),
        _sig("youtube", "Claude Design is Easy", {"claude", "design", "easy"}),
    ]
    assert len(ds.cross_channel_pairs(signals)) == 1


# --------------------------------------------------------------------------- #
# cross_channel_pairs — the ECHO candidates
# --------------------------------------------------------------------------- #

def _sig(channel, text, tokens):
    return {"channel": channel, "kind": "browsed", "text": text, "tokens": set(tokens)}


def test_cross_channel_pairs_finds_the_echo():
    signals = [
        _sig("chrome", "Inside PM at Stripe", {"stripe", "archetypes"}),
        _sig("youtube", "Inside PM at Stripe (video)", {"stripe", "archetypes"}),
    ]
    pairs = ds.cross_channel_pairs(signals)
    assert len(pairs) == 1
    assert pairs[0]["kind"] == "cross"
    assert pairs[0]["overlap"] == ["archetypes", "stripe"]


def test_cross_channel_pairs_ignores_same_channel():
    # Two pages on one site sharing a word is one thing seen once, not an echo.
    signals = [
        _sig("chrome", "Stripe docs", {"stripe", "billing"}),
        _sig("chrome", "Stripe pricing", {"stripe", "pricing"}),
    ]
    assert ds.cross_channel_pairs(signals) == []


def test_cross_channel_pairs_caps(monkeypatch):
    monkeypatch.setattr(ds, "MAX_CROSS_CANDIDATES", 1)
    signals = [
        _sig("chrome", "a", {"stripe", "archetypes"}),
        _sig("youtube", "b", {"stripe", "archetypes"}),
        _sig("ai-chat", "c", {"stripe", "archetypes"}),
    ]
    assert len(ds.cross_channel_pairs(signals)) == 1


def test_cross_channel_pairs_rejects_a_single_shared_token():
    # A GitHub repo page and the chat bullet "Committed all changes to `main`" became
    # an echo on the token "main" — one word is not a theme.
    signals = [
        _sig("chrome", "VoltAgent/awesome-design-md tree/main", {"voltagent", "design", "main"}),
        _sig("ai-chat", "Committed all changes to main", {"committed", "changes", "main"}),
    ]
    assert ds.cross_channel_pairs(signals) == []


def test_cross_channel_pairs_drops_the_same_artifact_twice():
    # A Liked video's own link is usually in the day's browsing too (youtu.be and the
    # newsletter redirectors are not in NOISE_DOMAINS), which is not an echo.
    title = {"claude", "design", "insanely", "easy", "even", "beginners"}
    signals = [
        _sig("chrome", "Claude Design is Insanely Easy (even for beginners) - YouTube",
             title | {"youtu"}),
        _sig("youtube", "Claude Design is Insanely Easy (even for beginners) — Jeff Su", title),
    ]
    assert ds.cross_channel_pairs(signals) == []


def test_cross_channel_pairs_keeps_two_formats_of_one_theme():
    # The case worth surfacing: his written tutorial and his video, same day.
    signals = [
        _sig("chrome", "Claude Design Tutorial: A Beginner's Guide (jeffsu.org)",
             {"claude", "design", "tutorial", "beginner", "guide"}),
        _sig("youtube", "Claude Design is Insanely Easy — Jeff Su",
             {"claude", "design", "insanely", "easy"}),
    ]
    pairs = ds.cross_channel_pairs(signals)
    assert len(pairs) == 1 and pairs[0]["overlap"] == ["claude", "design"]


def test_one_per_side_keeps_only_each_signal_s_best():
    # A Gemini browsing row matched google-gemini and google-gemini-api and took
    # three of five slots on real data.
    signal = _sig("chrome", "Google Gemini", {"google", "gemini"})
    pairs = ds.candidate_pairs([signal], [
        {"kind": "wiki page", "label": "google-gemini", "tokens": {"google", "gemini"}},
        {"kind": "wiki page", "label": "google-gemini-api", "tokens": {"google", "gemini", "api"}},
    ])
    assert len(pairs) == 1


def test_one_per_side_keeps_only_each_anchor_s_best():
    # One page matched a tutorial and the video of that same tutorial.
    anchor = {"kind": "wiki page", "label": "claude-knowledge-base-design",
              "tokens": {"claude", "design", "knowledge"}}
    pairs = ds.candidate_pairs([
        _sig("chrome", "Claude Design Tutorial", {"claude", "design", "tutorial"}),
        _sig("youtube", "Claude Design is Insanely Easy", {"claude", "design", "easy"}),
    ], [anchor])
    assert len(pairs) == 1


def test_render_candidates_labels_both_kinds():
    anchor_pair = {"kind": "anchor", "score": 1.0, "overlap": ["duckdb"],
                   "signal": _sig("chrome", "DuckDB docs", {"duckdb"}),
                   "anchor": {"kind": "wiki page", "label": "duckdb-analytics"}}
    cross_pair = {"kind": "cross", "score": 1.0, "overlap": ["stripe"],
                  "signal": _sig("chrome", "Stripe reading", {"stripe"}),
                  "other": _sig("youtube", "Stripe video", {"stripe"})}
    out = ds.render_candidates([anchor_pair, cross_pair])
    assert "1. CONNECTION" in out and "duckdb-analytics" in out
    assert "2. ECHO" in out and "Stripe video" in out


# --------------------------------------------------------------------------- #
# gather_anchors — pages carry their summary; activity logs and part-name company
# matches are excluded
# --------------------------------------------------------------------------- #

@pytest.fixture
def stub_anchors(monkeypatch):
    monkeypatch.setattr(ds, "get_watchlist", lambda: [])
    monkeypatch.setattr(ds, "list_opportunities", lambda **k: {"opportunities": []})


def test_gather_anchors_tokenizes_the_page_summary(stub_anchors, monkeypatch):
    # The point of the whole step: matching on the filename alone can only find
    # lexical identity, so "columnar" has to be able to reach duckdb-analytics.
    monkeypatch.setattr(ds, "page_summaries", lambda: {"pages": [
        {"name": "duckdb-analytics", "summary": "A columnar store for single-node work."}]})

    anchor = ds.gather_anchors(_LOG)[0]
    assert "columnar" in anchor["tokens"]
    assert anchor["summary"] == "A columnar store for single-node work."


def test_gather_anchors_skips_dated_activity_logs(stub_anchors, monkeypatch):
    # 49 of the vault's 203 pages are write-ups of the daily logs. Matching
    # yesterday's activity against the page written from yesterday's activity is
    # circular — on real data it produced exactly that.
    monkeypatch.setattr(ds, "page_summaries", lambda: {"pages": [
        {"name": "daily-chrome-2026-07-24", "summary": "A log of tools encountered."},
        {"name": "ai-chat-learnings-2026-07-01", "summary": "Chats from July 1."},
        {"name": "duckdb-analytics", "summary": "A columnar store."},
    ]})

    assert [a["label"] for a in ds.gather_anchors(_LOG)] == ["duckdb-analytics"]


def test_gather_anchors_reports_a_missing_vault(stub_anchors, monkeypatch, caplog):
    monkeypatch.setattr(ds, "page_summaries", lambda: {"error": "vault not found"})

    with caplog.at_level(logging.WARNING):
        assert ds.gather_anchors(_LOG) == []
    assert "vault not found" in caplog.text


def test_company_anchor_needs_its_whole_name(monkeypatch):
    # "Planet Fitness" matched the publication "UX Planet" on `planet` and, being a
    # two-token anchor, outranked every page in the vault.
    monkeypatch.setattr(ds, "page_summaries", lambda: {"pages": []})
    monkeypatch.setattr(ds, "get_watchlist", lambda: [{"company": "Planet Fitness"}])
    monkeypatch.setattr(ds, "list_opportunities", lambda **k: {"opportunities": []})
    anchors = ds.gather_anchors(_LOG)

    partial = _sig("chrome", "UX Planet article", {"planet", "article"})
    whole = _sig("chrome", "Planet Fitness opens a gym", {"planet", "fitness", "opens"})
    assert ds.candidate_pairs([partial], anchors) == []
    assert len(ds.candidate_pairs([whole], anchors)) == 1


def test_render_candidates_shows_the_anchor_summary():
    pair = {"kind": "anchor", "score": 1.0, "overlap": ["duckdb"],
            "signal": _sig("chrome", "DuckDB docs", {"duckdb"}),
            "anchor": {"kind": "wiki page", "label": "duckdb-analytics",
                       "summary": "A columnar store for single-node work."}}
    assert "— A columnar store for single-node work." in ds.render_candidates([pair])


# --------------------------------------------------------------------------- #
# _ai_chat_signals — the third channel, read from the 4:30 AM learnings log
# --------------------------------------------------------------------------- #

_AI_CHAT_LOG = """## AI Chat Learnings: July 24, 2026

### Claude · LocalLLMAgent · 5:20 AM
**Accomplished**
- Committed all changes to `main`.
**Learned**
- DuckDB analytics beats a warehouse for single-node work.

### Gemini · notes · 9:00 AM
**Accomplished**
- None
**Learned**
- Stripe PM archetypes shift from builder to operator.
"""


def _write_ai_chat_log(tmp_path, day, body=_AI_CHAT_LOG):
    """LEARNINGS_DIR is redirected to tmp_path by conftest, so this is where
    read_entry looks."""
    (tmp_path / f"AI-Chat-Learnings-{day:%Y-%m-%d}.md").write_text(body)


def _learned(*bullets):
    return "**Learned**\n" + "\n".join(f"- {b}" for b in bullets) + "\n"


def test_ai_chat_signals_takes_learned_bullets_only(tmp_path):
    # "Accomplished" is process — on real data those matched wiki pages on branch and
    # repo names, nothing more.
    _, _, day = ds.prior_day()
    _write_ai_chat_log(tmp_path, day)

    signals = ds._ai_chat_signals(day, _LOG)
    assert [s["text"] for s in signals] == [
        "DuckDB analytics beats a warehouse for single-node work.",
        "Stripe PM archetypes shift from builder to operator.",
    ]
    assert {s["channel"] for s in signals} == {"ai-chat"}
    assert "duckdb" in signals[0]["tokens"]


def test_ai_chat_signals_skips_none_markers(tmp_path):
    _, _, day = ds.prior_day()
    _write_ai_chat_log(tmp_path, day, _learned("None"))

    assert ds._ai_chat_signals(day, _LOG) == []


def test_ai_chat_signals_caps_bullets(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "MAX_AI_CHAT_BULLETS", 2)
    _, _, day = ds.prior_day()
    _write_ai_chat_log(tmp_path, day, _learned(*(f"topic number {i}" for i in range(10))))

    assert len(ds._ai_chat_signals(day, _LOG)) == 2


def test_ai_chat_signals_missing_file_yields_nothing(tmp_path):
    # The 4:30 task writes nothing on a day with no chats. Not a failure.
    _, _, day = ds.prior_day()
    assert ds._ai_chat_signals(day, _LOG) == []


def test_candidate_pairs_empty_without_overlap():
    signals = [{"kind": "browsed", "text": "x", "tokens": {"terraform"}}]
    anchors = [{"kind": "wiki page", "label": "cooking", "tokens": {"cooking"}}]
    assert ds.candidate_pairs(signals, anchors) == []


def test_parse_nudges_extracts_bullets_and_caps(monkeypatch):
    monkeypatch.setattr(ds, "MAX_NUDGES", 2)
    out = ds.parse_nudges("- one\n- two\n- three\nnot a bullet")
    assert out == ["one", "two"]


def test_parse_nudges_none_yields_nothing():
    assert ds.parse_nudges("NONE") == []
    assert ds.parse_nudges("") == []


# --------------------------------------------------------------------------- #
# drop_repeats
# --------------------------------------------------------------------------- #

def _anchor_pair(overlap, label, signal="something he did"):
    return {"kind": "anchor", "signal": {"text": signal, "kind": "browsed"},
            "anchor": {"label": label}, "score": 1.0, "overlap": overlap}


def test_drop_repeats_suppresses_the_same_anchor():
    # The real one, from the archive: 2026-08-03 nudged the omlx page off an
    # Ollama-runners chat, and 08-04 matched the same pair again.
    pair = _anchor_pair(["ollama"], "omlx", signal="Ollama runners and MLX")
    already = ["Your discussion of Ollama runners relates to your `omlx` notes; "
               "should I link them?"]

    assert ds.drop_repeats([pair], already) == []


def test_drop_repeats_ignores_the_overlap_terms_the_nudge_never_used():
    # Why the rule keys on the anchor, caught by a dry run against real data: the
    # matcher paired screenwatch-kit on {capture, logs}, and the nudge it was
    # repeating said "the `daily_synthesis.log` setup fits your `screenwatch-kit`
    # workflow" — sharing neither term. Requiring the overlap let the repeat through.
    pair = _anchor_pair(["capture", "logs"], "screenwatch-kit",
                        signal="logs/daily_synthesis.log will now capture output")
    already = ["The `daily_synthesis.log` setup fits your `screenwatch-kit` "
               "workflow; want to integrate them?"]

    assert ds.drop_repeats([pair], already) == []


def test_drop_repeats_keeps_a_different_anchor():
    pair = _anchor_pair(["duckdb"], "duckdb-analytics", signal="DuckDB deep dive")
    already = ["Your discussion of Ollama runners relates to your `omlx` notes."]

    assert ds.drop_repeats([pair], already) == [pair]


def test_drop_repeats_does_not_suppress_on_a_shared_word_alone():
    # A past nudge that merely says "Claude" must not kill every later Claude
    # candidate — the anchor is what scopes a repeat.
    pair = _anchor_pair(["claude"], "prompt-caching")
    already = ["Anthropic's context engineering updates may impact your Claude "
               "artifacts; want a summary?"]

    assert ds.drop_repeats([pair], already) == [pair]


def test_an_anchor_with_no_usable_tokens_falls_back_to_the_overlap():
    # An empty token set is a subset of everything, so without the fallback a
    # short-named anchor would suppress the whole shortlist.
    pair = _anchor_pair(["duckdb"], "n8n")
    already = ["Something about DuckDB entirely unrelated to that anchor."]

    assert ds.drop_repeats([pair], already) == []
    assert ds.drop_repeats([_anchor_pair(["kafka"], "n8n")], already) == [
        _anchor_pair(["kafka"], "n8n")]


def test_drop_repeats_matches_an_echo_on_its_shared_terms():
    # Echoes have no anchor, so the overlap is the whole identity — safe because
    # _MIN_ECHO_OVERLAP already demands two shared terms.
    pair = {"kind": "cross", "signal": {"text": "a", "kind": "browsed"},
            "other": {"text": "b", "kind": "watched"}, "score": 1.0,
            "overlap": ["claude", "code"]}
    already = ["Claude Code came at you twice yesterday — reading and video; worth a note?"]

    assert ds.drop_repeats([pair], already) == []


def test_drop_repeats_with_no_history_keeps_everything():
    pair = _anchor_pair(["duckdb"], "duckdb-analytics")
    assert ds.drop_repeats([pair], []) == [pair]


# --------------------------------------------------------------------------- #
# main() branches
# --------------------------------------------------------------------------- #

def test_genuine_overlap_pushes_one_nudge(stub_sources, monkeypatch):
    monkeypatch.setattr(ds, "fetch_liked_videos",
                        lambda *a, **k: {"videos": [{"title": "DuckDB deep dive", "channel": "X"}]})
    monkeypatch.setattr(ds, "page_summaries", lambda: {"pages": [{"name": "duckdb-analytics", "summary": ""}]})

    assert ds.main() == 0
    assert stub_sources["model"] == 1
    assert len(stub_sources["pushes"]) == 1
    assert "DuckDB" in stub_sources["pushes"][0]["message"]
    assert stub_sources["pushes"][0]["email_fallback"] is True


def test_genuine_overlap_writes_vault_entry(stub_sources, tmp_path, monkeypatch):
    # The durable archive: a dated Daily-Synthesis file lands in SYNTHESIS_DIR
    # (redirected to tmp_path by conftest) so suggestions survive the transient
    # push.
    monkeypatch.setattr(ds, "fetch_liked_videos",
                        lambda *a, **k: {"videos": [{"title": "DuckDB deep dive", "channel": "X"}]})
    monkeypatch.setattr(ds, "page_summaries", lambda: {"pages": [{"name": "duckdb-analytics", "summary": ""}]})

    assert ds.main() == 0
    files = list(tmp_path.glob("Daily-Synthesis-*.md"))
    assert len(files) == 1
    body = files[0].read_text()
    assert "Synthesis Suggestions" in body and "DuckDB" in body


def test_archive_goes_to_synthesis_dir_not_learnings_dir(stub_sources, tmp_path, monkeypatch):
    # The whole point of #1: the archive must never land in LEARNINGS_DIR (the
    # vault's raw/), which ObsidianWikiAgent ingests as sources — a nudge ingested
    # as a source becomes a fabricated wiki claim.
    raw, nudges = tmp_path / "raw", tmp_path / "nudges"
    raw.mkdir()
    nudges.mkdir()
    monkeypatch.setenv("LEARNINGS_DIR", str(raw))
    monkeypatch.setenv("SYNTHESIS_DIR", str(nudges))
    monkeypatch.setattr(ds, "fetch_liked_videos",
                        lambda *a, **k: {"videos": [{"title": "DuckDB deep dive", "channel": "X"}]})
    monkeypatch.setattr(ds, "page_summaries", lambda: {"pages": [{"name": "duckdb-analytics", "summary": ""}]})

    assert ds.main() == 0
    assert len(list(nudges.glob("Daily-Synthesis-*.md"))) == 1
    assert list(raw.iterdir()) == []


def test_no_overlap_pushes_nothing_and_skips_model(stub_sources, tmp_path, monkeypatch):
    # A signal and an anchor that share no tokens.
    monkeypatch.setattr(ds, "fetch_liked_videos",
                        lambda *a, **k: {"videos": [{"title": "Terraform basics", "channel": "X"}]})
    monkeypatch.setattr(ds, "page_summaries", lambda: {"pages": [{"name": "sourdough", "summary": ""}]})

    assert ds.main() == 0
    assert stub_sources["model"] == 0        # never warmed/queried the model
    assert stub_sources["pushes"] == []
    assert list(tmp_path.glob("Daily-Synthesis-*.md")) == []  # nothing to archive


def _duckdb_day(monkeypatch):
    """The scenario the push tests use: a Liked video that matches a wiki page."""
    monkeypatch.setattr(ds, "fetch_liked_videos",
                        lambda *a, **k: {"videos": [{"title": "DuckDB deep dive", "channel": "X"}]})
    monkeypatch.setattr(ds, "page_summaries",
                        lambda: {"pages": [{"name": "duckdb-analytics", "summary": ""}]})


def test_a_connection_already_nudged_is_not_pushed_again(stub_sources, tmp_path,
                                                         monkeypatch, caplog):
    # Yesterday's archive already made this exact connection. The run must stop
    # before the model — and say which kind of quiet morning it was, since "no
    # overlap" and "nothing new" look identical from the outside.
    _, _, day = ds.prior_day()
    (tmp_path / f"Daily-Synthesis-{day:%Y-%m-%d}.md").write_text(
        "## Synthesis Suggestions\n\n"
        "- You dug into DuckDB — it fits your 'duckdb-analytics' note; want a summary?\n"
    )
    _duckdb_day(monkeypatch)

    with caplog.at_level(logging.INFO):
        assert ds.main() == 0

    assert stub_sources["model"] == 0        # never warmed/queried the model
    assert stub_sources["pushes"] == []
    assert "were repeats of the last" in caplog.text


def test_an_unreadable_nudge_archive_costs_the_filter_not_the_run(stub_sources,
                                                                  monkeypatch, caplog):
    # Degrade, don't crash: losing the repeat filter must never lose the nudge.
    def _boom(**k):
        raise OSError("vault unmounted")
    monkeypatch.setattr(ds, "list_nudges", _boom)
    _duckdb_day(monkeypatch)

    with caplog.at_level(logging.WARNING):
        assert ds.main() == 0

    assert len(stub_sources["pushes"]) == 1
    assert "not suppressing repeats" in caplog.text


def test_a_nudge_outside_the_window_does_not_suppress(stub_sources, tmp_path, monkeypatch):
    # The filter only looks back RECENT_NUDGE_DAYS; an old connection is fair to
    # make again.
    _, _, day = ds.prior_day()
    stale = day - timedelta(days=ds.RECENT_NUDGE_DAYS + 5)
    (tmp_path / f"Daily-Synthesis-{stale:%Y-%m-%d}.md").write_text(
        "- You dug into DuckDB — it fits your 'duckdb-analytics' note; want a summary?\n"
    )
    _duckdb_day(monkeypatch)

    assert ds.main() == 0
    assert len(stub_sources["pushes"]) == 1


def test_echo_across_channels_reaches_the_model(stub_sources, tmp_path, monkeypatch):
    # The case no single-source pass can see: the same theme arrives via a Liked
    # video and an AI chat on the same day, with no wiki anchor involved at all.
    _, _, day = ds.prior_day()
    _write_ai_chat_log(tmp_path, day,
                       _learned("Stripe PM archetypes shift from builder to operator."))
    monkeypatch.setattr(ds, "fetch_liked_videos", lambda *a, **k: {"videos": [
        {"title": "Inside PM at Stripe: archetypes after builder", "channel": "The Skip"}]})

    seen = {}

    def _capture(**kwargs):
        seen["prompt"] = kwargs["user_prompt"]
        return "- Stripe archetypes came at you twice yesterday; worth a note?"
    monkeypatch.setattr(ds, "complete_text", _capture)

    assert ds.main() == 0
    assert "ECHO" in seen["prompt"]
    assert "stripe" in seen["prompt"]
    assert len(stub_sources["pushes"]) == 1


@pytest.fixture
def one_candidate(monkeypatch):
    """Shape a run that reaches the model with exactly one candidate, so a test
    only has to say what the model replied."""
    monkeypatch.setattr(ds, "fetch_liked_videos",
                        lambda *a, **k: {"videos": [{"title": "DuckDB deep dive", "channel": "X"}]})
    monkeypatch.setattr(ds, "page_summaries",
                        lambda: {"pages": [{"name": "duckdb-analytics", "summary": ""}]})


def test_model_says_none_pushes_nothing(stub_sources, one_candidate, monkeypatch, caplog):
    monkeypatch.setattr(ds, "complete_text", lambda **k: "NONE")

    with caplog.at_level(logging.WARNING):
        assert ds.main() == 0
    assert stub_sources["pushes"] == []
    # The healthy silence path stays quiet: a WARNING here would cry wolf every
    # ordinary day and get muted, taking the two real ones below with it.
    assert caplog.text == ""


def test_empty_model_reply_warns_rather_than_reading_as_a_quiet_day(
        stub_sources, one_candidate, monkeypatch, caplog):
    # An empty reply is what a thinking model that spent num_predict on scratchpad
    # returns. Without this it logged the same INFO as a genuine NONE, so a
    # permanently broken run looked exactly like a healthy one for weeks.
    monkeypatch.setattr(ds, "complete_text", lambda **k: "")

    with caplog.at_level(logging.WARNING):
        assert ds.main() == 0
    assert stub_sources["pushes"] == []
    assert "EMPTY content" in caplog.text
    assert "1 candidate(s)" in caplog.text


def test_an_empty_reply_is_asked_again_and_the_retry_is_what_ships(
        stub_sources, one_candidate, monkeypatch, caplog):
    # The measured failure: thinking spends num_predict on scratchpad and the
    # content comes back empty, discarding candidates that already matched. It's
    # nondeterministic, so the same prompt asked twice gets an answer — without the
    # retry, 3 of 29 real runs pushed nothing on a day that had something to say.
    replies = iter(["", "- a real connection"])
    monkeypatch.setattr(ds, "complete_text", lambda **k: next(replies))

    with caplog.at_level(logging.WARNING):
        assert ds.main() == 0
    assert len(stub_sources["pushes"]) == 1
    assert "a real connection" in stub_sources["pushes"][0]["message"]
    # The recovered attempt is still worth a WARNING: it reached the log_inspector
    # rollup, which is the only way a creeping failure rate gets noticed at all.
    assert "attempt 1/2" in caplog.text


def test_the_model_is_asked_exactly_twice_before_giving_up(
        stub_sources, one_candidate, monkeypatch, caplog):
    # Bounds the retry against the single Ollama slot: this runs at 5:45am and
    # every extra attempt is ~35s that chat and the other jobs queue behind.
    calls = []

    def _empty(**k):
        calls.append(k)
        return ""

    monkeypatch.setattr(ds, "complete_text", _empty)

    with caplog.at_level(logging.WARNING):
        assert ds.main() == 0
    assert len(calls) == ds.MAX_SYNTHESIS_ATTEMPTS == 2
    assert stub_sources["pushes"] == []
    assert "on all 2 attempt(s)" in caplog.text


def test_unparsable_model_reply_warns_with_the_counts(
        stub_sources, one_candidate, monkeypatch, caplog):
    # Non-empty, no "- " bullets, and no NONE: the model ignored the output format.
    # Distinct from empty, so the warning names the char count that empty can't.
    reply = "I found nothing of interest today."
    monkeypatch.setattr(ds, "complete_text", lambda **k: reply)

    with caplog.at_level(logging.WARNING):
        assert ds.main() == 0
    assert stub_sources["pushes"] == []
    assert "no parsable" in caplog.text
    assert f"{len(reply)} chars" in caplog.text
    assert "1 candidate(s)" in caplog.text


def test_dead_source_degrades_and_still_runs(stub_sources, monkeypatch):
    # Chrome history blows up; YouTube still yields an overlapping signal, so the
    # run completes and pushes rather than crashing on the dead source.
    def _boom(*a, **k):
        raise RuntimeError("chrome DB locked")
    monkeypatch.setattr(ds, "fetch_chrome_history", _boom)
    monkeypatch.setattr(ds, "fetch_liked_videos",
                        lambda *a, **k: {"videos": [{"title": "DuckDB deep dive", "channel": "X"}]})
    monkeypatch.setattr(ds, "page_summaries", lambda: {"pages": [{"name": "duckdb-analytics", "summary": ""}]})

    assert ds.main() == 0
    assert len(stub_sources["pushes"]) == 1


def test_company_anchor_matches_browsing(stub_sources, monkeypatch):
    # Anchors also come from the watchlist; a browsed page mentioning the company
    # is a candidate. Uses YouTube to avoid the compact_sites/prefs path.
    monkeypatch.setattr(ds, "fetch_liked_videos",
                        lambda *a, **k: {"videos": [{"title": "Acme launches a new API", "channel": "X"}]})
    monkeypatch.setattr(ds, "get_watchlist", lambda: [{"company": "Acme"}])

    assert ds.main() == 0
    assert len(stub_sources["pushes"]) == 1


# --------------------------------------------------------------------------- #
# project anchors — what he's building, merged with what he's written about it
# --------------------------------------------------------------------------- #

@pytest.fixture
def stub_projects(monkeypatch):
    """Default both halves of the project merge to empty. Tests override each."""
    monkeypatch.setattr(ds, "load_registry", lambda: [])
    monkeypatch.setattr(ds, "list_project_pages", lambda: {"projects": []})


def _project(name, summary="A cooperative word game.", topics=None):
    return {"name": name, "summary": summary,
            "topics": topics if topics is not None else ["sse", "lobby", "wordplay"]}


def test_project_anchors_carry_the_summary_and_topics(stub_anchors, stub_projects,
                                                      monkeypatch):
    # The whole point: an article on server-sent events has nothing to say to a
    # note about note-taking, but plenty to say to the repo that just moved to SSE.
    monkeypatch.setattr(ds, "page_summaries", lambda: {"pages": []})
    monkeypatch.setattr(ds, "load_registry", lambda: [_project("WeighAnchor")])

    anchor = ds.gather_anchors(_LOG)[0]
    assert anchor["kind"] == "project you're building"
    assert anchor["label"] == "WeighAnchor"
    assert {"cooperative", "word", "game"} <= anchor["tokens"]  # from the summary
    assert {"lobby", "wordplay"} <= anchor["tokens"]            # from the topics


def test_a_project_with_a_wiki_page_becomes_one_anchor_not_two(stub_anchors,
                                                               stub_projects,
                                                               monkeypatch):
    # _one_per_side dedupes by side identity, so two anchors for one project both
    # place and the model is shown the same story twice — the exact thing that
    # function exists to prevent.
    monkeypatch.setattr(ds, "page_summaries", lambda: {"pages": [
        {"name": "weigh-anchor", "summary": "Moved to server-side authority."},
        {"name": "duckdb-analytics", "summary": "A columnar store."},
    ]})
    monkeypatch.setattr(ds, "load_registry", lambda: [_project("WeighAnchor")])
    monkeypatch.setattr(ds, "list_project_pages", lambda: {"projects": [
        {"name": "weigh-anchor", "repo": "cjdube/WeighAnchor",
         "path": "WeighAnchor", "summary": "Moved to server-side authority."}]})

    anchors = ds.gather_anchors(_LOG)
    assert [a["label"] for a in anchors] == ["WeighAnchor", "duckdb-analytics"]
    # The wiki page's summary wins: it carries the decisions the README omits.
    assert anchors[0]["summary"] == "Moved to server-side authority."
    assert "authority" in anchors[0]["tokens"]


def test_the_join_is_on_frontmatter_not_the_slug(stub_anchors, stub_projects,
                                                 monkeypatch):
    # The page for the LocalLLMAgent checkout is called `wren`. Nothing about
    # those two strings matches, which is why the marker exists at all.
    monkeypatch.setattr(ds, "page_summaries", lambda: {"pages": [
        {"name": "wren", "summary": "A local-first personal agent."}]})
    monkeypatch.setattr(ds, "load_registry",
                        lambda: [_project("LocalLLMAgent", summary="An agent.")])
    monkeypatch.setattr(ds, "list_project_pages", lambda: {"projects": [
        {"name": "wren", "repo": "cjdube/LocalLLMAgent", "path": "LocalLLMAgent",
         "summary": "A local-first personal agent."}]})

    anchors = ds.gather_anchors(_LOG)
    assert [a["label"] for a in anchors] == ["LocalLLMAgent"]
    assert anchors[0]["summary"] == "A local-first personal agent."


def test_an_unmatched_wiki_project_page_stays_a_wiki_anchor(stub_anchors,
                                                            stub_projects,
                                                            monkeypatch):
    # A page marked as a project whose checkout is gone (or never scanned) must
    # not silently disappear from the corpus.
    monkeypatch.setattr(ds, "page_summaries", lambda: {"pages": [
        {"name": "agentos", "summary": "Pluggable agent configs."}]})
    monkeypatch.setattr(ds, "load_registry", lambda: [_project("WeighAnchor")])
    monkeypatch.setattr(ds, "list_project_pages", lambda: {"projects": [
        {"name": "agentos", "repo": "cjdube/AgentOS", "path": "AgentOS",
         "summary": "Pluggable agent configs."}]})

    assert {a["label"] for a in ds.gather_anchors(_LOG)} == {"WeighAnchor", "agentos"}


def test_a_project_with_nothing_but_a_name_is_skipped(stub_anchors, stub_projects,
                                                      monkeypatch):
    # Anchoring on a bare name can only match its own spelling — the tautology
    # gather_anchors' docstring warns about. project_scan already logged why
    # these have no summary (no README in the repo).
    monkeypatch.setattr(ds, "load_registry",
                        lambda: [_project("my-agent-hq", summary="", topics=[])])

    assert ds.gather_anchors(_LOG) == []


def test_project_anchor_tokens_are_capped(stub_anchors, stub_projects, monkeypatch):
    # The bug _ai_chat_signals documents: a token set large enough to overlap
    # everything outranks every real pair. A project has a README, a CLAUDE.md
    # and a docs tree behind it, so the bound is enforced, not assumed.
    monkeypatch.setattr(ds, "load_registry", lambda: [_project(
        "Huge",
        summary=" ".join(f"summaryword{i}" for i in range(200)),
        topics=[f"topicword{i}" for i in range(200)])])

    anchor = ds.gather_anchors(_LOG)[0]
    assert len(anchor["tokens"]) == ds.MAX_PROJECT_ANCHOR_TOKENS


def test_project_tokens_drop_the_topic_tail_not_the_name(monkeypatch):
    tokens = ds._project_tokens("weigh-anchor", "A cooperative word game.",
                                [f"topicword{i}" for i in range(200)])
    assert len(tokens) == ds.MAX_PROJECT_ANCHOR_TOKENS
    # Name and summary survive; the topic list is what gets truncated.
    assert {"weigh", "anchor", "cooperative", "word", "game"} <= tokens


def test_a_missing_project_registry_costs_only_the_project_anchors(stub_anchors,
                                                                   stub_projects,
                                                                   monkeypatch, caplog):
    monkeypatch.setattr(ds, "page_summaries", lambda: {"pages": [
        {"name": "duckdb-analytics", "summary": "A columnar store."}]})
    monkeypatch.setattr(ds, "load_registry",
                        lambda: (_ for _ in ()).throw(OSError("store unreadable")))

    with caplog.at_level(logging.WARNING):
        assert [a["label"] for a in ds.gather_anchors(_LOG)] == ["duckdb-analytics"]
    assert "store unreadable" in caplog.text


def test_an_unreadable_vault_still_leaves_the_project_anchors(stub_anchors,
                                                              stub_projects,
                                                              monkeypatch, caplog):
    monkeypatch.setattr(ds, "load_registry", lambda: [_project("WeighAnchor")])
    monkeypatch.setattr(ds, "list_project_pages",
                        lambda: (_ for _ in ()).throw(OSError("vault gone")))

    with caplog.at_level(logging.WARNING):
        anchors = ds.gather_anchors(_LOG)
    assert [a["label"] for a in anchors] == ["WeighAnchor"]
    assert "not merging" in caplog.text


def test_a_verbose_summary_cannot_crowd_out_the_topics(stub_anchors, stub_projects,
                                                       monkeypatch):
    # Real case: the wiki page for the LocalLLMAgent checkout ran to 30 words
    # ("...modeled after the high-output, agile characteristics of the wren
    # bird") — more than the name and all ten topics combined — and pushed
    # `rest`, `tailscale` and `tool` out of the anchor. A page describing the
    # project badly was displacing terms taken from the repo itself.
    monkeypatch.setattr(ds, "load_registry", lambda: [_project(
        "Thing",
        summary=" ".join(f"padding{i}" for i in range(80)),
        topics=["tailscale", "ollama", "launchd"])])

    tokens = ds.gather_anchors(_LOG)[0]["tokens"]
    assert {"tailscale", "ollama", "launchd"} <= tokens
    assert len(tokens) < ds.MAX_PROJECT_ANCHOR_TOKENS  # the cap is now a backstop


def test_summary_head_cuts_at_a_word_boundary():
    # A mid-word slice leaves a fragment that is a real token: "tailsca" matches
    # nothing but still occupies a slot.
    summary = "word " * 200
    head = ds._summary_head(summary)
    assert len(head) <= ds.MAX_ANCHOR_SUMMARY_CHARS
    assert not head.endswith("wor")
    assert all(t == "word" for t in ds._tokenize(head))


def test_summary_head_leaves_a_short_summary_alone():
    assert ds._summary_head("A cooperative word game.") == "A cooperative word game."
