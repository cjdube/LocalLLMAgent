"""Tests for chat/wikireview.py — placing vault pages in groups, and saved marks.

The page names here are invented. This repo is public, and the real group list
lives only in the gitignored config/wiki_review.json.
"""

import pytest

from agent.store import atomic_write_json, load_json
from chat import wikireview as wr


GROUPS = [
    {"id": "tools", "family": "Topics", "title": "Tools", "line": "Tool pages.",
     "pages": ["alpha", "with space", "gone"]},
    {"id": "d-daily", "family": "Dated logs", "title": "Daily", "line": "One a day.",
     "prefix": "daily-note"},
    {"id": "empty", "family": "Topics", "title": "Empty", "line": "All gone.",
     "pages": ["also-gone"]},
]


@pytest.fixture
def vault(tmp_path, monkeypatch):
    wiki = tmp_path / "vault" / "wiki"
    wiki.mkdir(parents=True)
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path / "vault"))
    for name in ("alpha", "with space", "daily-note-2026-01-02", "new-page", "index", "log"):
        (wiki / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")
    return wiki


@pytest.fixture
def store():
    atomic_write_json(wr._STORE_PATH, {"groups": GROUPS, "marks": {}, "sorted_at": "2026-01-03"})


def _by_id(result):
    return {g["id"]: g for g in result["groups"]}


def test_every_page_lands_in_exactly_one_group(vault, store):
    result = wr.review()
    placed = [name for g in result["groups"] for name in g["pages"]]
    assert sorted(placed) == sorted(result["pages"])
    assert len(placed) == len(set(placed))


def test_index_and_log_are_not_pages(vault, store):
    assert not {"index", "log"} & set(wr.review()["pages"])


def test_a_name_gone_from_the_vault_drops_out_and_takes_an_empty_group_with_it(vault, store):
    groups = _by_id(wr.review())
    assert groups["tools"]["pages"] == ["alpha", "with space"]
    assert "empty" not in groups


def test_a_new_page_lands_in_unsorted(vault, store):
    assert _by_id(wr.review())[wr.UNSORTED_ID]["pages"] == ["new-page"]


def test_a_dated_page_is_placed_by_prefix(vault, store):
    assert _by_id(wr.review())["d-daily"]["pages"] == ["daily-note-2026-01-02"]


def test_a_prefix_never_claims_a_page_with_no_date(vault, store):
    (vault / "daily-note-howto.md").write_text("# howto\n", encoding="utf-8")
    assert "daily-note-howto" in _by_id(wr.review())[wr.UNSORTED_ID]["pages"]


def test_no_store_yet_puts_everything_in_unsorted(vault):
    result = wr.review()
    assert [g["id"] for g in result["groups"]] == [wr.UNSORTED_ID]
    assert result["sorted_at"] == ""


def test_missing_vault_is_an_error_not_a_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path / "nowhere"))
    assert "error" in wr.review()


def test_page_facts(vault, store):
    (vault / "alpha.md").write_text(
        "# Alpha\n\n"
        "**Summary**: About [[other|the other page]] and [[beta]].\n\n"
        "**Sources**: raw/a-2026-01-01.md, raw/b-2026-03-04.md\n\n"
        "**Last updated**: 2026-09-09\n\nBody words.\n",
        encoding="utf-8")
    summary, words, sources, inbound, date = wr.review()["pages"]["alpha"]
    assert summary == "About the other page and beta."
    assert words == len((vault / "alpha.md").read_text().split())
    assert sources == 2
    assert inbound == 0
    assert date == "2026-03-04"          # the newest source beats Last updated


def test_date_falls_back_to_last_updated(vault, store):
    (vault / "alpha.md").write_text("# A\n\n**Last updated**: 2026-09-09\n", encoding="utf-8")
    assert wr.review()["pages"]["alpha"][4] == "2026-09-09"


def test_summary_is_capped_at_220_chars(vault, store):
    (vault / "alpha.md").write_text("# A\n\n**Summary**: " + "x" * 300 + "\n", encoding="utf-8")
    assert len(wr.review()["pages"]["alpha"][0]) == 220


def test_inbound_counts_pages_not_links(vault, store):
    (vault / "alpha.md").write_text("[[new-page]] [[new-page]] [[alpha]]\n", encoding="utf-8")
    (vault / "with space.md").write_text("[[New-Page|alias]]\n", encoding="utf-8")
    pages = wr.review()["pages"]
    assert pages["new-page"][3] == 2     # two pages, case folded, duplicate counted once
    assert pages["alpha"][3] == 0        # a self-link is not a dependency


def test_a_link_inside_a_code_span_does_not_count(vault, store):
    (vault / "alpha.md").write_text("`[[new-page]]`\n", encoding="utf-8")
    assert wr.review()["pages"]["new-page"][3] == 0


def test_mark_saves_and_reads_back(vault, store):
    wr.set_mark("tools", "cut", {"alpha": "keep"})
    assert wr.review()["marks"]["tools"] == {"verdict": "cut", "pages": {"alpha": "keep"},
                                             "covers": ["alpha", "with space"]}
    assert load_json(wr._STORE_PATH, {})["groups"] == GROUPS     # the save keeps the rest


def test_clearing_a_mark_removes_it(vault, store):
    wr.set_mark("tools", "cut", {})
    wr.set_mark("tools", None, {})
    assert wr.review()["marks"] == {}


def test_a_mark_on_a_page_since_cut_is_not_reported(vault, store):
    wr.set_mark("tools", "cut", {"alpha": "keep", "with space": "cut"})
    (vault / "alpha.md").unlink()
    assert wr.review()["marks"]["tools"]["pages"] == {"with space": "cut"}


@pytest.mark.parametrize("group_id, new_page", [
    ("d-daily", "daily-note-2026-01-03"),   # a dated group gains each new daily page
    (wr.UNSORTED_ID, "another-new-page"),   # Unsorted gains every new page
])
def test_a_verdict_does_not_cover_a_page_that_joins_later(vault, store, group_id, new_page):
    wr.set_mark(group_id, "cut", {})
    before = _by_id(wr.review())[group_id]["pages"]
    (vault / f"{new_page}.md").write_text("# new\n", encoding="utf-8")
    result = wr.review()
    assert new_page in _by_id(result)[group_id]["pages"]
    assert result["marks"][group_id]["covers"] == before


def test_a_tick_keeps_the_covered_list_and_a_new_verdict_refreshes_it(vault, store):
    wr.set_mark("d-daily", "cut", {})
    (vault / "daily-note-2026-01-03.md").write_text("# new\n", encoding="utf-8")
    wr.set_mark("d-daily", "cut", {"daily-note-2026-01-02": "keep"})
    assert wr.review()["marks"]["d-daily"]["covers"] == ["daily-note-2026-01-02"]
    wr.set_mark("d-daily", "unsure", {})
    assert wr.review()["marks"]["d-daily"]["covers"] == ["daily-note-2026-01-02", "daily-note-2026-01-03"]


def test_overrides_with_no_verdict_cover_nothing(vault, store):
    wr.set_mark("tools", None, {"alpha": "cut"})
    assert wr.review()["marks"]["tools"]["covers"] == []


@pytest.mark.parametrize("verdict", ["maybe", 1, ["cut"]])
def test_bad_verdict_is_refused(vault, store, verdict):
    with pytest.raises(ValueError):
        wr.set_mark("tools", verdict, {})


@pytest.mark.parametrize("pages", [["alpha"], {"alpha": "unsure"}, {"alpha": ["cut"]}])
def test_bad_page_marks_are_refused(vault, store, pages):
    with pytest.raises(ValueError):
        wr.set_mark("tools", "cut", pages)


def test_unknown_group_is_refused(vault, store):
    with pytest.raises(LookupError):
        wr.set_mark("nope", "cut", {})


def test_page_outside_the_group_is_refused(vault, store):
    with pytest.raises(LookupError):
        wr.set_mark("tools", "cut", {"new-page": "cut"})


def test_a_refused_mark_changes_nothing(vault, store):
    with pytest.raises(LookupError):
        wr.set_mark("tools", "cut", {"new-page": "cut"})
    assert load_json(wr._STORE_PATH, {})["marks"] == {}


def test_marks_never_touch_the_vault(vault, store):
    before = {p.name: p.read_bytes() for p in vault.iterdir()}
    wr.set_mark("tools", "cut", {})
    assert {p.name: p.read_bytes() for p in vault.iterdir()} == before


def test_move_takes_a_page_out_of_unsorted_and_survives_a_read(vault, store):
    wr.move_page("new-page", "tools")
    groups = _by_id(wr.review())
    assert "new-page" in groups["tools"]["pages"]
    assert "unsorted" not in groups


def test_move_into_a_group_with_no_pages_left_brings_it_back(vault, store):
    wr.move_page("new-page", "empty")
    assert _by_id(wr.review())["empty"]["pages"] == ["new-page"]


def test_move_targets_leave_out_dated_groups(vault, store):
    assert [t["id"] for t in wr.review()["move_to"]] == ["tools", "empty"]


def test_move_refuses_a_dated_group(vault, store):
    # The message matters: without the guard, the missing "pages" key is a KeyError,
    # which is also a LookupError.
    with pytest.raises(LookupError, match="cannot take a page"):
        wr.move_page("new-page", "d-daily")


def test_move_refuses_a_page_that_is_already_placed(vault, store):
    with pytest.raises(LookupError):
        wr.move_page("alpha", "empty")
    assert _by_id(wr.review())["tools"]["pages"][0] == "alpha"


def test_a_cut_tick_follows_the_page_so_the_cut_list_does_not_change(vault, store):
    wr.set_mark("unsorted", None, {"new-page": "cut"})
    wr.move_page("new-page", "tools")
    marks = load_json(wr._STORE_PATH, {})["marks"]
    assert marks["tools"] == {"verdict": None, "pages": {"new-page": "cut"}, "covers": []}
    assert marks["unsorted"]["pages"] == {}


def test_a_group_verdict_on_the_target_is_kept(vault, store):
    wr.set_mark("tools", "keep", {})
    wr.set_mark("unsorted", None, {"new-page": "cut"})
    wr.move_page("new-page", "tools")
    assert load_json(wr._STORE_PATH, {})["marks"]["tools"]["verdict"] == "keep"


def test_move_never_touches_the_vault(vault, store):
    before = {p.name: p.read_text() for p in vault.iterdir()}
    wr.move_page("new-page", "tools")
    assert {p.name: p.read_text() for p in vault.iterdir()} == before
