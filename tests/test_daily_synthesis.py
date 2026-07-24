"""Tests for tasks/daily_synthesis.py — the pure matching/parsing helpers, and
main()'s branches: a genuine overlap pushes one nudge, no overlap or a NONE
model reply pushes nothing, and a dead source degrades instead of crashing.

Every external source (Chrome history, YouTube Likes, wiki, opportunities) and
the model/warm/notify calls are stubbed — no Chrome DB, no Google, no model, no
push. TIMEZONE is pinned so the prior-day window is deterministic, not the
host's zone (CLAUDE.md: UTC→local day windows)."""

import pytest

from tasks import daily_synthesis as ds

# LEARNINGS_DIR (the vault's raw/) is redirected to tmp_path suite-wide by
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

    monkeypatch.setattr(ds, "fetch_chrome_history", lambda *a, **k: {"sites": []})
    monkeypatch.setattr(ds, "fetch_liked_videos", lambda *a, **k: {"videos": []})
    monkeypatch.setattr(ds, "list_wiki_pages", lambda: {"pages": []})
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
    assert pairs[0]["score"] == 2


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
# main() branches
# --------------------------------------------------------------------------- #

def test_genuine_overlap_pushes_one_nudge(stub_sources, monkeypatch):
    monkeypatch.setattr(ds, "fetch_liked_videos",
                        lambda *a, **k: {"videos": [{"title": "DuckDB deep dive", "channel": "X"}]})
    monkeypatch.setattr(ds, "list_wiki_pages", lambda: {"pages": ["duckdb-analytics.md"]})

    assert ds.main() == 0
    assert stub_sources["model"] == 1
    assert len(stub_sources["pushes"]) == 1
    assert "DuckDB" in stub_sources["pushes"][0]["message"]
    assert stub_sources["pushes"][0]["email_fallback"] is True


def test_genuine_overlap_writes_vault_entry(stub_sources, tmp_path, monkeypatch):
    # The durable archive: a dated Daily-Synthesis file lands in the vault's raw/
    # (LEARNINGS_DIR, redirected to tmp_path by conftest) so suggestions survive
    # the transient push.
    monkeypatch.setattr(ds, "fetch_liked_videos",
                        lambda *a, **k: {"videos": [{"title": "DuckDB deep dive", "channel": "X"}]})
    monkeypatch.setattr(ds, "list_wiki_pages", lambda: {"pages": ["duckdb-analytics.md"]})

    assert ds.main() == 0
    files = list(tmp_path.glob("Daily-Synthesis-*.md"))
    assert len(files) == 1
    body = files[0].read_text()
    assert "Synthesis Suggestions" in body and "DuckDB" in body


def test_no_overlap_pushes_nothing_and_skips_model(stub_sources, tmp_path, monkeypatch):
    # A signal and an anchor that share no tokens.
    monkeypatch.setattr(ds, "fetch_liked_videos",
                        lambda *a, **k: {"videos": [{"title": "Terraform basics", "channel": "X"}]})
    monkeypatch.setattr(ds, "list_wiki_pages", lambda: {"pages": ["sourdough.md"]})

    assert ds.main() == 0
    assert stub_sources["model"] == 0        # never warmed/queried the model
    assert stub_sources["pushes"] == []
    assert list(tmp_path.glob("Daily-Synthesis-*.md")) == []  # nothing to archive


def test_model_says_none_pushes_nothing(stub_sources, monkeypatch):
    monkeypatch.setattr(ds, "fetch_liked_videos",
                        lambda *a, **k: {"videos": [{"title": "DuckDB deep dive", "channel": "X"}]})
    monkeypatch.setattr(ds, "list_wiki_pages", lambda: {"pages": ["duckdb-analytics.md"]})
    monkeypatch.setattr(ds, "complete_text", lambda **k: "NONE")

    assert ds.main() == 0
    assert stub_sources["pushes"] == []


def test_dead_source_degrades_and_still_runs(stub_sources, monkeypatch):
    # Chrome history blows up; YouTube still yields an overlapping signal, so the
    # run completes and pushes rather than crashing on the dead source.
    def _boom(*a, **k):
        raise RuntimeError("chrome DB locked")
    monkeypatch.setattr(ds, "fetch_chrome_history", _boom)
    monkeypatch.setattr(ds, "fetch_liked_videos",
                        lambda *a, **k: {"videos": [{"title": "DuckDB deep dive", "channel": "X"}]})
    monkeypatch.setattr(ds, "list_wiki_pages", lambda: {"pages": ["duckdb-analytics.md"]})

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
