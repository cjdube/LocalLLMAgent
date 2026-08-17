"""Tests for chat/routes_wiki.py — the /wiki JSON API.

run_lint is stubbed in every test (conftest blocks the real one outright, since
it spawns the sibling lint repo and can write to a vault). What's covered here is
the Flask edge: shapes, status codes, and the traversal guard on page reads.

The auth sweep in tests/test_server.py already asserts every route here answers
401 to an unauthenticated caller, so that isn't repeated.
"""

import os

os.environ.setdefault("WREN_CHAT_TOKEN", "test-token")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import pytest

from chat import routes_wiki as rw
from chat import server as srv
from chat import wikilint


FINDINGS = {"vault": "/v", "pages": 3, "findings": 1, "fixes": [],
            "sections": {"Orphan pages": ["a.md is an orphan."], "Page format": []}}


@pytest.fixture
def auth_client():
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:
        with c.session_transaction() as sess:
            sess["authenticated"] = True
            sess["sid"] = "test-sid"
        yield c


@pytest.fixture
def vault(tmp_path, monkeypatch):
    wiki = tmp_path / "vault" / "wiki"
    wiki.mkdir(parents=True)
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path / "vault"))
    return wiki


def _stub_lint(monkeypatch, result, calls=None):
    def _fake(fix=False):
        if calls is not None:
            calls.append(fix)
        return result
    monkeypatch.setattr(wikilint, "run_lint", _fake)


# --------------------------------------------------------------------------- #
# GET /api/wiki/lint
# --------------------------------------------------------------------------- #

def test_lint_returns_the_sections(auth_client, monkeypatch):
    _stub_lint(monkeypatch, FINDINGS)
    body = auth_client.get("/api/wiki/lint").get_json()
    assert body["pages"] == 3
    assert body["sections"]["Orphan pages"] == ["a.md is an orphan."]
    # Clean sections survive the round trip, so the page can show what was checked.
    assert body["sections"]["Page format"] == []


def test_a_broken_lint_repo_is_a_200_with_an_error(auth_client, monkeypatch):
    """The view renders the error as its own state; a 500 would just blank it."""
    _stub_lint(monkeypatch, {"error": "wiki_lint.py not found"})
    resp = auth_client.get("/api/wiki/lint")
    assert resp.status_code == 200
    assert "not found" in resp.get_json()["error"]


def test_a_read_never_asks_for_fixes(auth_client, monkeypatch):
    calls = []
    _stub_lint(monkeypatch, FINDINGS, calls)
    auth_client.get("/api/wiki/lint")
    assert calls == [False]


# --------------------------------------------------------------------------- #
# POST /api/wiki/lint/fix
# --------------------------------------------------------------------------- #

def test_fix_runs_with_fix_and_returns_the_change_log(auth_client, monkeypatch):
    calls = []
    _stub_lint(monkeypatch, dict(FINDINGS, fixes=["a.md: removed 1 self-link"]), calls)
    body = auth_client.post("/api/wiki/lint/fix").get_json()
    assert calls == [True]
    assert body["fixes"] == ["a.md: removed 1 self-link"]


def test_fix_is_logged(auth_client, monkeypatch, caplog):
    """The only write path Wren has into the vault. An unlogged vault edit is
    indistinguishable from an ingest bug."""
    _stub_lint(monkeypatch, dict(FINDINGS, fixes=["a.md: removed 1 self-link"]))
    with caplog.at_level("INFO", logger="wren"):
        auth_client.post("/api/wiki/lint/fix")
    assert "removed 1 self-link" in caplog.text


def test_fix_is_not_a_get(auth_client, monkeypatch):
    _stub_lint(monkeypatch, FINDINGS)
    assert auth_client.get("/api/wiki/lint/fix").status_code == 405


# --------------------------------------------------------------------------- #
# GET /api/wiki/graph
# --------------------------------------------------------------------------- #

def test_graph_returns_nodes_and_edges(auth_client, vault):
    (vault / "a.md").write_text("# A\n\n**Summary**: first\n\n[[b]]\n")
    (vault / "b.md").write_text("# B\n\n**Summary**: second\n")
    body = auth_client.get("/api/wiki/graph").get_json()
    assert [n["id"] for n in body["nodes"]] == ["a", "b"]
    assert body["edges"] == [[0, 1]]
    assert body["dangling"] == 0


def test_graph_reports_a_missing_vault_without_a_500(auth_client, tmp_path, monkeypatch):
    from chat import wikigraph
    wikigraph._GRAPH_CACHE.clear()
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path / "gone"))
    resp = auth_client.get("/api/wiki/graph")
    assert resp.status_code == 200
    assert "vault not found" in resp.get_json()["error"]
    wikigraph._GRAPH_CACHE.clear()


# --------------------------------------------------------------------------- #
# GET /api/wiki/page/<name>
# --------------------------------------------------------------------------- #

def test_page_returns_the_whole_file(auth_client, vault):
    """Not read_wiki_page: that applies _fit_page, a budget for the model's
    context window. A human reading on their phone has no such budget."""
    body = "# A\n\n" + ("long prose. " * 3000)
    (vault / "a.md").write_text(body)
    resp = auth_client.get("/api/wiki/page/a")
    assert resp.status_code == 200
    assert resp.get_json()["content"] == body


def test_page_accepts_an_explicit_md_suffix(auth_client, vault):
    (vault / "a.md").write_text("# A\n")
    assert auth_client.get("/api/wiki/page/a.md").status_code == 200


def test_unknown_page_is_a_404(auth_client, vault):
    assert auth_client.get("/api/wiki/page/nope").status_code == 404


def test_traversal_cannot_read_outside_the_wiki_dir(auth_client, vault, tmp_path):
    """Two locks. Flask's default converter won't match a '/' in <name>, so this
    404s at routing before the handler sees it; _safe_child is the second lock,
    for a name that reaches the handler some other way. Either way the file
    outside wiki/ is never served."""
    (tmp_path / "vault" / "RULES.md").write_text("secret")
    resp = auth_client.get("/api/wiki/page/..%2FRULES.md")
    assert resp.status_code != 200
    assert "secret" not in resp.get_data(as_text=True)


def test_the_handler_guards_the_path_itself(auth_client, vault, tmp_path, monkeypatch):
    """_safe_child is wired in, not just imported — proved by making the routing
    layer hand the handler a name it would otherwise never see."""
    (tmp_path / "vault" / "RULES.md").write_text("secret")
    monkeypatch.setattr(rw, "_authenticated", lambda: True)
    with srv.app.test_request_context():
        resp = rw.api_wiki_page("../RULES.md")
    assert resp[1] == 400
    assert "not a page" in resp[0].get_json()["error"]


def test_missing_vault_is_a_404(auth_client, tmp_path, monkeypatch):
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path / "gone"))
    resp = auth_client.get("/api/wiki/page/a")
    assert resp.status_code == 404
    assert "vault not found" in resp.get_json()["error"]
