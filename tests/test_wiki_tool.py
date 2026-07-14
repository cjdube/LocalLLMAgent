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
    missing = tmp_path / "not-mounted"
    monkeypatch.setenv("WIKI_VAULT_PATH", str(missing))

    for result in (wiki.read_wiki_index(), wiki.list_wiki_pages(),
                   wiki.read_wiki_page("x")):
        assert "error" in result and "not found" in result["error"]
    assert not missing.exists()  # never created the mount point on the boot disk


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
