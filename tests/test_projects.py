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


def test_reads_readme_claude_md_and_doc_headings(root):
    _repo(root, "Alpha", {
        "README.md": "# Alpha\nDoes alpha things.",
        "CLAUDE.md": "Alpha's house rules.",
        "docs/design.md": "# The design of Alpha\n\nbody",
        "docs/no-heading.md": "just a body",
    })

    row = _by_name(projects.scan_projects())["Alpha"]
    assert "Does alpha things." in row["readme"]
    assert row["claude_md"] == "Alpha's house rules."
    # First heading when there is one, the filename stem when there isn't.
    assert row["doc_titles"] == ["The design of Alpha", "no-heading"]


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
    assert row["readme"] == "" and row["claude_md"] == "" and row["doc_titles"] == []


def test_stray_files_and_dot_dirs_are_not_projects(root):
    _repo(root, "Alpha", {"README.md": "# Alpha"}, commit=False)
    (root / "notes.md").write_text("a blog draft the user keeps here")
    (root / ".claude").mkdir()

    assert list(_by_name(projects.scan_projects())) == ["Alpha"]


def test_never_reads_anything_but_readme_claude_md_and_docs(root):
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
