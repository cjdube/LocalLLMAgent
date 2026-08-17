"""Tests for chat/wikigraph.py — the learnings wiki as a link graph.

Everything runs against a throwaway vault under tmp_path; conftest redirects
WIKI_VAULT_PATH suite-wide, and each test here points it at its own fixture.

Two behaviours carry real weight. Link parsing must match the sibling repo's,
or /wiki and /wiki/lint disagree about the same file — the vault contains a page
about wiki-link syntax that a naive regex mis-parses, which is what the backtick
case below pins. And index.md must stay out: it links every page, so as a node it
is a 388-degree hub that flattens the layout.
"""

import os

os.environ.setdefault("WREN_CHAT_TOKEN", "test-token")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import pytest

from chat import wikigraph as wg


def page(title="A Page", summary="what it is", updated="2026-08-01", body="", front=""):
    return (
        f"{front}"
        f"# {title}\n\n"
        f"**Summary**: {summary}\n"
        f"**Sources**: note.txt\n"
        f"**Last updated**: {updated}\n\n"
        f"{body}\n"
    )


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A vault helper; `.page(slug, content)` seeds one wiki page."""
    root = tmp_path / "vault"
    wiki = root / "wiki"
    wiki.mkdir(parents=True)
    monkeypatch.setenv("WIKI_VAULT_PATH", str(root))
    wg._GRAPH_CACHE.clear()

    class V:
        path = root
        dir = wiki

        def page(self, slug, content):
            (wiki / f"{slug}.md").write_text(content, encoding="utf-8")

    yield V()
    wg._GRAPH_CACHE.clear()


def by_id(graph):
    return {n["id"]: n for n in graph["nodes"]}


def slugs(graph, edge):
    return tuple(sorted(graph["nodes"][i]["id"] for i in edge))


# --------------------------------------------------------------------------- #
# link parsing
# --------------------------------------------------------------------------- #

def test_link_targets_strips_alias_anchor_and_suffix():
    assert wg.link_targets("[[a]] [[b|Bee]] [[c#Section]] [[d.md]]") == ["a", "b", "c", "d"]


def test_link_targets_ignores_a_code_span():
    """The real failure this guards. ai-chat-learnings-2026-08-02.md in the live
    vault discusses wiki-link syntax; without excluding backticks a code span's
    '[[' opens a match that runs through the prose and closes on the ']]' of the
    genuine link after it — inventing one target and losing the real one."""
    text = "`[[` brackets can cause the [[obsidian-wiki-agent]] page trouble"
    assert wg.link_targets(text) == ["obsidian-wiki-agent"]


def test_link_targets_does_not_run_past_a_newline():
    assert wg.link_targets("[[unclosed\nand [[real]] here") == ["real"]


# --------------------------------------------------------------------------- #
# nodes
# --------------------------------------------------------------------------- #

def test_nodes_carry_title_summary_and_date(vault):
    vault.page("ollama", page(title="Ollama & Model Config", summary="Local runner.",
                              updated="2026-08-03"))
    node = by_id(wg.build_graph())["ollama"]
    assert node["title"] == "Ollama & Model Config"
    assert node["summary"] == "Local runner."
    assert node["updated"] == "2026-08-03"


def test_a_long_summary_is_trimmed(vault):
    vault.page("a", page(summary="x" * 400))
    node = by_id(wg.build_graph())["a"]
    assert len(node["summary"]) == wg.MAX_SUMMARY_CHARS
    assert node["summary"].endswith("…")


def test_kinds_are_classified(vault):
    vault.page("plain", page())
    vault.page("daily-chrome-2026-08-16", page(title="Chrome, 16 Aug"))
    vault.page("ai-slop", page(front="---\nlens: true\ndescription: judge a draft\n---\n"))
    vault.page("wren", page(front="---\nproject: true\nrepo: x\npath: /x\n---\n"))
    kinds = {k: v["kind"] for k, v in by_id(wg.build_graph()).items()}
    assert kinds == {"plain": "concept", "daily-chrome-2026-08-16": "log",
                     "ai-slop": "lens", "wren": "project"}


def test_a_lens_that_is_also_dated_is_still_a_lens(vault):
    """Kind order matters: the dated test is a naming convention, the frontmatter
    marker is a deliberate declaration."""
    vault.page("review-2026-08-16", page(front="---\nlens: true\n---\n"))
    assert by_id(wg.build_graph())["review-2026-08-16"]["kind"] == "lens"


def test_degree_counts_both_directions(vault):
    vault.page("hub", page(body="[[a]] [[b]]"))
    vault.page("a", page(body="[[hub]]"))
    vault.page("b", page())
    nodes = by_id(wg.build_graph())
    assert nodes["hub"]["deg"] == 2
    assert nodes["a"]["deg"] == 1
    assert nodes["b"]["deg"] == 1


# --------------------------------------------------------------------------- #
# edges
# --------------------------------------------------------------------------- #

def test_a_reciprocal_pair_is_one_edge(vault):
    """The view is undirected. Drawing A→B and B→A separately just darkens the
    line and doubles both degrees."""
    vault.page("a", page(body="[[b]]"))
    vault.page("b", page(body="[[a]]"))
    graph = wg.build_graph()
    assert len(graph["edges"]) == 1
    assert by_id(graph)["a"]["deg"] == 1


def test_repeated_links_are_one_edge(vault):
    vault.page("a", page(body="[[b]] and again [[b]] and [[b]]"))
    vault.page("b", page())
    assert len(wg.build_graph()["edges"]) == 1


def test_a_self_link_is_not_an_edge(vault):
    vault.page("a", page(body="see [[a]]"))
    graph = wg.build_graph()
    assert graph["edges"] == []
    assert by_id(graph)["a"]["deg"] == 0


def test_a_link_to_a_missing_page_is_counted_not_drawn(vault):
    """An invented target is a lint finding. Materialising it would put a page on
    the map that is not in the vault."""
    vault.page("a", page(body="[[nope]] [[also-nope]]"))
    graph = wg.build_graph()
    assert graph["edges"] == []
    assert graph["dangling"] == 2
    assert "nope" not in by_id(graph)


def test_edges_are_index_pairs_into_nodes(vault):
    vault.page("a", page(body="[[b]]"))
    vault.page("b", page())
    graph = wg.build_graph()
    assert slugs(graph, graph["edges"][0]) == ("a", "b")


# --------------------------------------------------------------------------- #
# what is left out
# --------------------------------------------------------------------------- #

def test_index_and_log_are_not_nodes(vault):
    """index.md links every page in the vault. As a node it is a hub with an edge
    to everything, which pulls the layout into a starburst and hides every real
    relationship."""
    vault.page("index", "# Index\n\n- [[a]]\n- [[b]]\n")
    vault.page("log", "# Log\n\n- did things\n")
    vault.page("a", page(body="[[b]]"))
    vault.page("b", page())
    graph = wg.build_graph()
    assert set(by_id(graph)) == {"a", "b"}
    assert len(graph["edges"]) == 1


def test_a_link_to_the_index_is_not_dangling_noise(vault):
    # index.md exists but is not a node, so a link to it is dropped like any
    # other unresolvable target. It counts, which is honest — the lint would not
    # flag it, so the two views differ here by one; the count is a hint, not an
    # alarm.
    vault.page("index", "# Index\n")
    vault.page("a", page(body="[[index]]"))
    assert wg.build_graph()["dangling"] == 1


def test_a_missing_vault_is_an_error_not_a_crash(tmp_path, monkeypatch):
    wg._GRAPH_CACHE.clear()
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path / "gone"))
    assert "vault not found" in wg.build_graph()["error"]


def test_an_empty_wiki_is_an_empty_graph(vault):
    graph = wg.build_graph()
    assert graph["nodes"] == [] and graph["edges"] == [] and graph["dangling"] == 0


# --------------------------------------------------------------------------- #
# caching
# --------------------------------------------------------------------------- #

def test_repeat_builds_are_served_from_cache(vault, monkeypatch):
    vault.page("a", page())
    wg.build_graph()
    monkeypatch.setattr(wg, "_build_graph", lambda: {"error": "should not rebuild"})
    assert "error" not in wg.build_graph()


def test_editing_a_page_invalidates_the_cache(vault):
    vault.page("a", page(summary="before"))
    assert by_id(wg.build_graph())["a"]["summary"] == "before"
    os.utime(vault.dir / "a.md", (0, 0))
    vault.page("a", page(summary="after"))
    assert by_id(wg.build_graph())["a"]["summary"] == "after"


def test_adding_a_page_invalidates_the_cache(vault):
    vault.page("a", page())
    assert len(wg.build_graph()["nodes"]) == 1
    vault.page("b", page())
    assert len(wg.build_graph()["nodes"]) == 2


def test_a_failed_build_is_not_cached(tmp_path, monkeypatch):
    wg._GRAPH_CACHE.clear()
    root = tmp_path / "vault"
    monkeypatch.setenv("WIKI_VAULT_PATH", str(root))
    assert "error" in wg.build_graph()
    (root / "wiki").mkdir(parents=True)
    (root / "wiki" / "a.md").write_text(page(), encoding="utf-8")
    assert len(wg.build_graph()["nodes"]) == 1
