"""Tests for agent.tools.projects — the deterministic scan of local checkouts.

PROJECTS_DIR is read fresh in _projects_dir() on every call, so pointing it at a
tmp_path fully isolates these from the developer's real ~/Projects (conftest
pins it at an empty tmp dir for the rest of the suite).

The scan shells out to real git against real fixture repos rather than
monkeypatching subprocess: git is a hard dependency of the module, the commands
are the part most likely to be wrong, and a stub would only assert that the
stub was called.
"""

import subprocess

import pytest

from agent.tools import projects


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True,
                   capture_output=True, text=True)


def _repo(root, name, files=None, commit=True):
    """A fixture checkout: a directory, optional files, optionally a git repo
    with one commit on a deterministic branch."""
    path = root / name
    path.mkdir(parents=True)
    for filename, content in (files or {}).items():
        target = path / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    if commit:
        _git(path, "init", "-b", "main")
        _git(path, "config", "user.email", "test@example.com")
        _git(path, "config", "user.name", "Test")
        _git(path, "add", "-A")
        _git(path, "-c", "commit.gpgsign=false", "commit", "-m", "initial",
             "--allow-empty")
    return path


@pytest.fixture
def root(tmp_path, monkeypatch):
    d = tmp_path / "Projects"
    d.mkdir()
    monkeypatch.setenv("PROJECTS_DIR", str(d))
    return d


def _by_name(result):
    return {p["name"]: p for p in result["projects"]}


def test_missing_projects_dir_is_an_error_not_a_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "nope"))
    assert "error" in projects.scan_projects()


def test_reads_readme_agent_instructions_and_doc_headings(root):
    _repo(root, "Alpha", {
        "README.md": "# Alpha\nDoes alpha things.",
        "AGENTS.md": "Alpha's house rules.",
        "docs/design.md": "# The design of Alpha\n\nbody",
        "docs/no-heading.md": "just a body",
    })

    row = _by_name(projects.scan_projects())["Alpha"]
    assert "Does alpha things." in row["readme"]
    assert row["agent_instructions"] == "Alpha's house rules."
    # First heading when there is one, the filename stem when there isn't.
    assert row["doc_titles"] == ["The design of Alpha", "no-heading"]


def test_agents_md_counts_as_agent_instructions(root):
    # AgenticDevelopment carries AGENTS.md and no README, so before this it
    # scanned as undocumented and got no anchor at all — a whole project the
    # synthesis could never nudge about.
    _repo(root, "Agentic", {"AGENTS.md": "Agentic's house rules."}, commit=False)

    assert _by_name(projects.scan_projects())["Agentic"]["agent_instructions"] \
        == "Agentic's house rules."


def test_configured_instruction_filenames_use_declared_order(root, monkeypatch):
    monkeypatch.setattr(projects.prefs, "project_instruction_files",
                        lambda: ("SECOND.md", "FIRST.md"))
    _repo(root, "Both", {"FIRST.md": "first file",
                         "SECOND.md": "second file"}, commit=False)

    assert _by_name(projects.scan_projects())["Both"]["agent_instructions"] == "second file"


def test_reports_git_freshness(root):
    _repo(root, "Alpha", {"README.md": "# Alpha"})

    row = _by_name(projects.scan_projects())["Alpha"]
    assert row["branch"] == "main"
    assert row["last_commit"]  # a bare ISO day from %cs, never sliced
    assert row["commits_30d"] == 1
    assert row["dirty"] is False


def test_uncommitted_changes_are_reported_dirty(root):
    path = _repo(root, "Alpha", {"README.md": "# Alpha"})
    (path / "README.md").write_text("# Alpha, edited")

    assert _by_name(projects.scan_projects())["Alpha"]["dirty"] is True


def test_a_non_git_directory_degrades_to_null_git_fields(root):
    # 5 of the user's 12 checkouts have no git at all — an ordinary outcome, not
    # an error, and the docs must still be read.
    _repo(root, "Plain", {"README.md": "# Plain\nNo git here."}, commit=False)

    row = _by_name(projects.scan_projects())["Plain"]
    assert row["remote"] is None and row["branch"] is None
    assert row["last_commit"] is None and row["dirty"] is None
    assert "No git here." in row["readme"]


def test_a_directory_with_no_docs_still_produces_a_row(root):
    _repo(root, "Bare", {"main.py": "print(1)"}, commit=False)

    row = _by_name(projects.scan_projects())["Bare"]
    assert row["readme"] == "" and row["agent_instructions"] == ""
    assert row["doc_titles"] == []


def test_stray_files_and_dot_dirs_are_not_projects(root):
    _repo(root, "Alpha", {"README.md": "# Alpha"}, commit=False)
    (root / "notes.md").write_text("a blog draft the user keeps here")
    (root / ".tooling").mkdir()

    assert list(_by_name(projects.scan_projects())) == ["Alpha"]


def test_never_reads_anything_but_readme_configured_instructions_and_docs(root):
    # The registry is written to a store and travels into prompts. A project
    # directory routinely holds secrets (SortOfCardGame has a .env), so the read
    # list is exhaustive on purpose — this pins it.
    _repo(root, "Alpha", {
        "README.md": "# Alpha",
        ".env": "OPENAI_API_KEY=sk-SHOULD-NEVER-APPEAR",
        "config/settings.json": '{"token": "SHOULD-NEVER-APPEAR"}',
        "secrets.txt": "SHOULD-NEVER-APPEAR",
    }, commit=False)

    assert "SHOULD-NEVER-APPEAR" not in str(projects.scan_projects())


def test_readme_is_capped(root, monkeypatch):
    monkeypatch.setattr(projects, "DOC_CHARS", 10)
    _repo(root, "Alpha", {"README.md": "x" * 500}, commit=False)

    assert len(_by_name(projects.scan_projects())["Alpha"]["readme"]) == 10


def test_doc_titles_are_capped(root, monkeypatch):
    monkeypatch.setattr(projects, "MAX_DOC_TITLES", 3)
    _repo(root, "Alpha", {f"docs/page{i}.md": f"# Page {i}" for i in range(10)},
          commit=False)

    assert len(_by_name(projects.scan_projects())["Alpha"]["doc_titles"]) == 3


def test_capping_doc_titles_reports_how_many_were_found(root, monkeypatch):
    """The cap is fine; the cap being invisible is not. docs_found carries the
    pre-cap count so project_scan can say the tail was dropped."""
    monkeypatch.setattr(projects, "MAX_DOC_TITLES", 3)
    _repo(root, "Alpha", {f"docs/page{i}.md": f"# Page {i}" for i in range(10)},
          commit=False)

    row = _by_name(projects.scan_projects())["Alpha"]
    assert row["docs_found"] == 10
    assert len(row["doc_titles"]) == 3


def test_docs_found_equals_titles_when_under_the_cap(root):
    _repo(root, "Alpha", {"docs/one.md": "# One", "docs/two.md": "# Two"},
          commit=False)

    row = _by_name(projects.scan_projects())["Alpha"]
    assert row["docs_found"] == len(row["doc_titles"]) == 2


def test_a_project_with_no_docs_dir_reports_zero_found(root):
    _repo(root, "Alpha", {"README.md": "# Alpha"})

    row = _by_name(projects.scan_projects())["Alpha"]
    assert row["docs_found"] == 0 and row["doc_titles"] == []


def test_outgrowing_the_cap_does_not_change_the_content_hash(root, monkeypatch):
    """Re-distilling every project because one gained a doc past the cap would
    cost a model call per project for no change in what any of them is."""
    monkeypatch.setattr(projects, "MAX_DOC_TITLES", 2)
    _repo(root, "Alpha", {"docs/a.md": "# A", "docs/b.md": "# B"}, commit=False)
    before = _by_name(projects.scan_projects())["Alpha"]["content_hash"]

    (root / "Alpha" / "docs" / "c.md").write_text("# C")

    assert _by_name(projects.scan_projects())["Alpha"]["content_hash"] == before


def test_content_hash_tracks_the_docs_not_the_commits(root):
    path = _repo(root, "Alpha", {"README.md": "# Alpha"})
    before = _by_name(projects.scan_projects())["Alpha"]["content_hash"]

    # A commit that touches no documentation must not invalidate the blurb cache.
    (path / "main.py").write_text("print(1)")
    _git(path, "add", "-A")
    _git(path, "-c", "commit.gpgsign=false", "commit", "-m", "code only")
    assert _by_name(projects.scan_projects())["Alpha"]["content_hash"] == before

    (path / "README.md").write_text("# Alpha\nNow it does something else.")
    assert _by_name(projects.scan_projects())["Alpha"]["content_hash"] != before


def test_content_hash_tracks_instruction_content_not_its_filename(root, monkeypatch):
    path = _repo(root, "Alpha", {
        "FIRST.md": "Same guidance.",
        "SECOND.md": "Same guidance.",
    }, commit=False)
    monkeypatch.setattr(projects.prefs, "project_instruction_files",
                        lambda: ("FIRST.md", "SECOND.md"))
    before = _by_name(projects.scan_projects())["Alpha"]["content_hash"]

    monkeypatch.setattr(projects.prefs, "project_instruction_files",
                        lambda: ("SECOND.md", "FIRST.md"))
    assert _by_name(projects.scan_projects())["Alpha"]["content_hash"] == before

    (path / "SECOND.md").write_text("Changed guidance.")
    assert _by_name(projects.scan_projects())["Alpha"]["content_hash"] != before


def test_one_unreadable_checkout_does_not_cost_the_others(root, monkeypatch):
    _repo(root, "Alpha", {"README.md": "# Alpha"}, commit=False)
    _repo(root, "Broken", {"README.md": "# Broken"}, commit=False)

    original = projects._scan_one

    def _explode(path):
        if path.name == "Broken":
            raise OSError("disk gave up")
        return original(path)
    monkeypatch.setattr(projects, "_scan_one", _explode)

    rows = _by_name(projects.scan_projects())
    assert "Alpha" in rows and "Broken" in rows
    assert "disk gave up" in rows["Broken"]["error"]
    assert rows["Alpha"]["readme"].startswith("# Alpha")


def test_a_hung_git_command_degrades_rather_than_raising(root, monkeypatch):
    _repo(root, "Alpha", {"README.md": "# Alpha"})

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=projects.GIT_TIMEOUT)
    monkeypatch.setattr(projects.subprocess, "run", _timeout)

    row = _by_name(projects.scan_projects())["Alpha"]
    assert row["last_commit"] is None
    assert row["readme"].startswith("# Alpha")


# --- the chat tools ---------------------------------------------------------
# Both read the checkouts LIVE and merge the cached distillation, so the git
# facts a chat answer quotes are true now rather than up to 24 hours stale.

def _seed_registry(monkeypatch, tmp_path, *entries):
    from agent.store import atomic_write_json
    path = tmp_path / "projects.json"
    monkeypatch.setattr(projects, "PROJECTS_PATH", path)
    atomic_write_json(path, {"projects": list(entries)})


def test_list_projects_merges_the_cached_summary_onto_a_live_scan(root, monkeypatch, tmp_path):
    _repo(root, "WeighAnchor", {"README.md": "# Weigh Anchor"})
    _seed_registry(monkeypatch, tmp_path,
                   {"name": "WeighAnchor", "summary": "A word-deduction game.",
                    "topics": ["sse", "lobby"]})

    row = projects.list_projects()["projects"][0]
    assert row["name"] == "WeighAnchor"
    assert row["summary"] == "A word-deduction game."
    assert row["last_commit"] and row["dirty"] is False   # live, not cached
    # The list stays compact: detail is read_project's job.
    assert "topics" not in row and "readme" not in row


def test_list_projects_without_a_registry_still_lists_them(root, monkeypatch, tmp_path):
    # The scan has never run. The projects are still real and their git state is
    # still true; they just have no summary yet.
    _repo(root, "WeighAnchor", {"README.md": "# Weigh Anchor"}, commit=False)
    monkeypatch.setattr(projects, "PROJECTS_PATH", tmp_path / "absent.json")

    assert projects.list_projects()["projects"][0]["summary"] == ""


def test_read_project_returns_the_detail(root, monkeypatch, tmp_path):
    _repo(root, "WeighAnchor", {"README.md": "# Weigh Anchor",
                                "docs/design.md": "# Engine design"})
    _seed_registry(monkeypatch, tmp_path,
                   {"name": "WeighAnchor", "summary": "A word game.",
                    "topics": ["sse", "lobby"]})
    monkeypatch.setattr(projects, "list_project_pages", lambda: {"projects": []})

    project = projects.read_project("WeighAnchor")
    assert project["summary"] == "A word game."
    assert project["topics"] == ["sse", "lobby"]
    assert project["doc_titles"] == ["Engine design"]
    assert project["branch"] == "main"
    assert project["wiki_page"] is None
    # The document bodies are never handed to the model here.
    assert "readme" not in project and "agent_instructions" not in project


def test_read_project_attaches_the_wiki_page(root, monkeypatch, tmp_path):
    # Joined on the page's `path:` frontmatter, not its name — the page for the
    # LocalLLMAgent checkout is called `wren`.
    _repo(root, "LocalLLMAgent", {"README.md": "# Wren"}, commit=False)
    _seed_registry(monkeypatch, tmp_path, {"name": "LocalLLMAgent", "summary": "An agent."})
    monkeypatch.setattr(projects, "list_project_pages", lambda: {"projects": [
        {"name": "wren", "repo": "cjdube/LocalLLMAgent", "path": "LocalLLMAgent",
         "summary": "Why it was built local-first."}]})

    project = projects.read_project("LocalLLMAgent")
    assert project["wiki_page"] == {"name": "wren",
                                    "summary": "Why it was built local-first."}


def test_read_project_survives_a_missing_vault(root, monkeypatch, tmp_path):
    _repo(root, "WeighAnchor", {"README.md": "# Weigh Anchor"}, commit=False)
    _seed_registry(monkeypatch, tmp_path, {"name": "WeighAnchor", "summary": "A word game."})

    def _boom():
        raise OSError("vault gone")
    monkeypatch.setattr(projects, "list_project_pages", _boom)

    assert projects.read_project("WeighAnchor")["wiki_page"] is None


def test_read_project_is_forgiving_about_case_and_spacing(root, monkeypatch, tmp_path):
    # The model is passing back a name the user typed, not an identifier it was
    # handed, so "weigh anchor" has to find WeighAnchor.
    _repo(root, "WeighAnchor", {"README.md": "# Weigh Anchor"}, commit=False)
    monkeypatch.setattr(projects, "list_project_pages", lambda: {"projects": []})

    for spelling in ("WeighAnchor", "weighanchor", "weigh anchor", "Weigh-Anchor"):
        assert projects.read_project(spelling)["name"] == "WeighAnchor"


def test_read_project_names_the_real_projects_when_it_misses(root, monkeypatch, tmp_path):
    # The error is what stops the model inventing a second guess: it gets the
    # actual list back instead of a bare "not found".
    _repo(root, "WeighAnchor", {"README.md": "# Weigh Anchor"}, commit=False)

    result = projects.read_project("Wordle")
    assert "no project named 'Wordle'" in result["error"]
    assert "WeighAnchor" in result["error"]


def test_read_project_rejects_an_empty_name(root):
    assert "error" in projects.read_project("")
    assert "error" in projects.read_project("   ")


def test_list_projects_description_forbids_inventing_one():
    # Measured on list_games, whose failure this shares: with a description that
    # only said WHEN to call it, the model answered a vague ask from pretraining
    # in 2 of 12 replays and invented entries with fabricated links. The risk is
    # worse here — a made-up project name reads as completely ordinary and
    # nothing downstream can catch it. Pinned so a future trim can't quietly
    # drop the denial.
    description = projects.LIST_PROJECTS_SCHEMA["function"]["description"]
    assert "NOT something you know" in description
    assert "Never name a project from your own knowledge" in description
    assert "never invent a repository" in description
