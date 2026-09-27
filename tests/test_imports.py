"""Tests for the CSV-upload store/tools. IMPORTS_DIR and _INDEX_PATH are
redirected to tmp_path, so nothing touches real config/imports.json or
config/imports/."""

import json

import pytest

from agent.tools import imports


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setattr(imports, "IMPORTS_DIR", tmp_path / "imports")
    monkeypatch.setattr(imports, "_INDEX_PATH", tmp_path / "imports.json")


def _csv_bytes(text: str) -> bytes:
    return text.encode("utf-8")


def test_real_max_import_bytes_is_5mib():
    assert imports.MAX_IMPORT_BYTES == 5 * 1024 * 1024


def test_txt_extension_rejected():
    out = imports.save_import("notes.txt", _csv_bytes("a,b\n1,2\n"))
    assert "error" in out


def test_oversized_file_rejected(monkeypatch):
    monkeypatch.setattr(imports, "MAX_IMPORT_BYTES", 10)
    data = _csv_bytes("a,b\n1,2\n3,4\n")  # well over 10 bytes
    assert len(data) > 10
    out = imports.save_import("big.csv", data)
    assert "error" in out


def test_empty_file_rejected():
    out = imports.save_import("empty.csv", b"")
    assert "error" in out


def test_latin1_bytes_accepted_and_read_back():
    data = "name,note\nCafe,Caf\xe9\n".encode("latin-1")
    saved = imports.save_import("cafe.csv", data)
    assert "error" not in saved
    assert saved["rows"] == 1
    page = imports.read_import(1)
    assert page["rows"] == [["Cafe", "Café"]]


def test_semicolon_delimiter_sniffed():
    data = _csv_bytes("name;amount\nRent;1200\nFood;300\n")
    saved = imports.save_import("bank.csv", data)
    assert "error" not in saved
    assert saved["columns"] == ["name", "amount"]
    assert saved["rows"] == 2
    page = imports.read_import(1)
    assert page["columns"] == ["name", "amount"]
    assert page["rows"] == [["Rent", "1200"], ["Food", "300"]]


def test_path_traversal_filename_stored_as_basename(tmp_path):
    imports.save_import("../../etc/x.csv", _csv_bytes("a,b\n1,2\n"))
    listed = imports.list_imports()
    assert listed["imports"][0]["name"] == "x.csv"
    for f in imports.IMPORTS_DIR.iterdir():
        assert f.resolve().parent == imports.IMPORTS_DIR.resolve()


def test_pruning_drops_oldest_file_and_row(monkeypatch):
    monkeypatch.setattr(imports, "MAX_IMPORTS", 3)
    for i in range(4):
        imports.save_import(f"file{i}.csv", _csv_bytes(f"a\n{i}\n"))

    listed = imports.list_imports()
    assert listed["count"] == 3
    names = [entry["name"] for entry in listed["imports"]]
    assert "file0.csv" not in names
    assert names == ["file3.csv", "file2.csv", "file1.csv"]

    idx = json.loads(imports._INDEX_PATH.read_text())
    ids_on_disk = {p.stem for p in imports.IMPORTS_DIR.iterdir()}
    ids_in_index = {entry["id"] for entry in idx["imports"]}
    assert ids_on_disk == ids_in_index
    assert len(ids_on_disk) == 3


def test_read_import_pages_every_row_exactly_once(monkeypatch):
    monkeypatch.setattr(imports, "READ_CHAR_BUDGET", 20)
    monkeypatch.setattr(imports, "READ_ROW_CAP", 100)
    rows_in = [f"row{i}" for i in range(10)]
    csv_text = "col\n" + "\n".join(rows_in) + "\n"
    imports.save_import("many.csv", _csv_bytes(csv_text))

    seen = []
    start_row = 1
    pages = 0
    while True:
        pages += 1
        assert pages < 50  # guard against an infinite loop on a bug
        page = imports.read_import(1, start_row)
        assert "error" not in page
        seen.extend(r[0] for r in page["rows"])
        if page["next_start_row"] is None:
            break
        start_row = page["next_start_row"]

    assert seen == rows_in
    assert pages > 1  # the small budget actually forced multiple pages


def test_read_import_note_says_not_whole_file_midway(monkeypatch):
    monkeypatch.setattr(imports, "READ_CHAR_BUDGET", 10)
    monkeypatch.setattr(imports, "READ_ROW_CAP", 100)
    csv_text = "col\n" + "\n".join(f"row{i}" for i in range(5)) + "\n"
    imports.save_import("many.csv", _csv_bytes(csv_text))

    page = imports.read_import(1)
    assert page["next_start_row"] is not None
    assert "NOT the whole file" in page["note"]
    assert f"start_row={page['next_start_row']}" in page["note"]


def test_read_import_end_of_file_note():
    imports.save_import("small.csv", _csv_bytes("col\nrow0\nrow1\n"))
    page = imports.read_import(1)
    assert page["next_start_row"] is None
    assert "end of file" in page["note"]


def test_read_import_bad_n_returns_error():
    imports.save_import("small.csv", _csv_bytes("col\nrow0\n"))
    assert "error" in imports.read_import(0)
    assert "error" in imports.read_import(-1)
    assert "error" in imports.read_import(99)
    assert "error" in imports.read_import("not-a-number")


def test_read_import_string_n_works():
    imports.save_import("small.csv", _csv_bytes("col\nrow0\n"))
    page = imports.read_import("1", start_row="1")
    assert "error" not in page
    assert page["rows"] == [["row0"]]


def test_list_imports_never_contains_id():
    imports.save_import("small.csv", _csv_bytes("col\nrow0\n"))
    idx = json.loads(imports._INDEX_PATH.read_text())
    real_id = idx["imports"][0]["id"]

    listed = imports.list_imports()
    assert real_id not in json.dumps(listed)

    page = imports.read_import(1)
    assert real_id not in json.dumps(page)


def test_list_imports_empty_has_note():
    listed = imports.list_imports()
    assert listed["count"] == 0
    assert listed["imports"] == []
    assert "note" in listed
    assert "paperclip" in listed["note"]


def test_read_import_missing_file_on_disk_returns_error():
    imports.save_import("small.csv", _csv_bytes("col\nrow0\n"))
    for f in imports.IMPORTS_DIR.iterdir():
        f.unlink()
    out = imports.read_import(1)
    assert "error" in out


def test_read_import_start_row_is_the_row_people_count():
    """"Row 50" means the 50th data row. With a 0-based offset the live model
    passed 50 and showed rows 51-56, 3 of 3 times."""
    imports.save_import("many.csv", _csv_bytes("col\n" + "\n".join(f"row{i}" for i in range(1, 61)) + "\n"))
    page = imports.read_import(1, start_row=50)
    assert page["rows"][0] == ["row50"]
    assert page["start_row"] == 50
    assert page["note"].startswith("Showing rows 50")
    assert "error" in imports.read_import(1, start_row=0)
    assert "error" in imports.read_import(1, start_row=61)
