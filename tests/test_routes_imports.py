"""Tests for chat/routes_imports.py — the /api/imports upload/list API.

conftest.py redirects agent.tools.imports.IMPORTS_DIR and ._INDEX_PATH to
tmp_path suite-wide, so nothing here reads or writes the real config/.
"""

import io
import os

os.environ.setdefault("WREN_CHAT_TOKEN", "test-token")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import pytest

from agent.tools import imports as imports_module
from chat import routes_imports as ri
from chat import server as srv


@pytest.fixture
def auth_client():
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:
        with c.session_transaction() as sess:
            sess["authenticated"] = True
            sess["sid"] = "test-sid"
        yield c


@pytest.fixture
def client():
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:
        yield c


_CSV = b"a,b,c\n1,2,3\n4,5,6\n"


# --------------------------------------------------------------------------- #
# auth gating
# --------------------------------------------------------------------------- #

def test_post_requires_auth(client):
    resp = client.post(
        "/api/imports",
        data={"file": (io.BytesIO(_CSV), "data.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "not authenticated"}


def test_get_requires_auth(client):
    resp = client.get("/api/imports")
    assert resp.status_code == 401
    assert resp.get_json() == {"error": "not authenticated"}


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #

def test_upload_then_list(auth_client, tmp_path):
    resp = auth_client.post(
        "/api/imports",
        data={"file": (io.BytesIO(_CSV), "data.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["n"] == 1
    assert body["name"] == "data.csv"
    assert body["rows"] == 2
    assert body["columns"] == ["a", "b", "c"]

    # The saved file lands under the redirected tmp_path dir, never the repo's
    # own config/ — this is the whole point of the conftest redirect.
    saved = list(imports_module.IMPORTS_DIR.glob("*.csv"))
    assert len(saved) == 1
    assert str(tmp_path) in str(imports_module.IMPORTS_DIR)

    listed = auth_client.get("/api/imports")
    assert listed.status_code == 200
    imports_list = listed.get_json()["imports"]
    assert len(imports_list) == 1
    assert imports_list[0]["name"] == "data.csv"


# --------------------------------------------------------------------------- #
# rejected uploads
# --------------------------------------------------------------------------- #

def test_non_csv_upload_is_rejected(auth_client):
    resp = auth_client.post(
        "/api/imports",
        data={"file": (io.BytesIO(b"hello"), "notes.txt")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_missing_file_field_is_rejected(auth_client):
    resp = auth_client.post("/api/imports", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_empty_filename_is_rejected(auth_client):
    resp = auth_client.post(
        "/api/imports",
        data={"file": (io.BytesIO(_CSV), "")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()


# --------------------------------------------------------------------------- #
# oversize body — the raise is scoped to this blueprint
# --------------------------------------------------------------------------- #

def test_oversize_body_is_413(auth_client, monkeypatch):
    # Patch the module constant the blueprint's before_request actually reads,
    # and re-derive MAX_IMPORTS_BODY_BYTES from it so the test stays fast.
    monkeypatch.setattr(imports_module, "MAX_IMPORT_BYTES", 100)
    monkeypatch.setattr(ri, "MAX_IMPORTS_BODY_BYTES", 100 + 64 * 1024)

    big = b"a,b\n" + b"1,2\n" * 20000  # well over 100 + 64KiB
    resp = auth_client.post(
        "/api/imports",
        data={"file": (io.BytesIO(big), "big.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 413


def test_chat_keeps_the_tighter_body_cap(auth_client):
    # The larger cap is scoped to this blueprint only; chat must not inherit it.
    resp = auth_client.post("/chat", json={"message": "x" * 400_000})
    assert resp.status_code == 413


# --------------------------------------------------------------------------- #
# PDF upload
# --------------------------------------------------------------------------- #

def test_pdf_upload_returns_kind_and_pages(auth_client):
    from tests.test_imports import make_pdf

    resp = auth_client.post(
        "/api/imports",
        data={"file": (io.BytesIO(make_pdf(["one", "two"])), "doc.pdf")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["kind"] == "pdf"
    assert body["pages"] == 2


def test_garbage_pdf_upload_is_rejected(auth_client):
    resp = auth_client.post(
        "/api/imports",
        data={"file": (io.BytesIO(b"not a real pdf"), "fake.pdf")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()
