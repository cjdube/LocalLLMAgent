"""Tests for the CSV/PDF-upload store/tools. IMPORTS_DIR and _INDEX_PATH are
redirected to tmp_path, so nothing touches real config/imports.json or
config/imports/."""

import io
import json

import pytest

from agent.tools import imports


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setattr(imports, "IMPORTS_DIR", tmp_path / "imports")
    monkeypatch.setattr(imports, "_INDEX_PATH", tmp_path / "imports.json")


def _csv_bytes(text: str) -> bytes:
    return text.encode("utf-8")


def make_pdf(pages: list) -> bytes:
    """Build a minimal, valid multi-page PDF by hand: a Catalog, a Pages tree,
    one Page + content-stream object per string in `pages`, a Helvetica Type1
    font, and a correct xref with real byte offsets. An empty string page has
    an empty content stream (no text-show operator), so pypdf extracts no
    text from it — used to test the "no text layer" rejection.

    Verified against pypdf directly (not just through save_import) before
    relying on it here: pypdf.PdfReader(io.BytesIO(make_pdf(["hi"]))) yields
    one page whose extract_text() returns "hi".
    """
    n = len(pages)
    # Object numbers: 1=Catalog, 2=Pages, 3=Font, then per page i (0-based):
    # page obj = 4+2*i, content obj = 5+2*i.
    objects = {}
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objects[1] = "<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {n} >>"
    objects[3] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    for i, text in enumerate(pages):
        page_obj = 4 + 2 * i
        content_obj = 5 + 2 * i
        objects[page_obj] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_obj} 0 R >>"
        )
        if text:
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET"
        else:
            stream = ""
        objects[content_obj] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"

    max_obj = 5 + 2 * (n - 1) if n else 3
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = {}
    for num in range(1, max_obj + 1):
        if num not in objects:
            continue
        offsets[num] = buf.tell()
        buf.write(f"{num} 0 obj\n".encode("latin-1"))
        buf.write(objects[num].encode("latin-1"))
        buf.write(b"\nendobj\n")

    xref_offset = buf.tell()
    count = max_obj + 1
    buf.write(f"xref\n0 {count}\n".encode("latin-1"))
    buf.write(b"0000000000 65535 f \n")
    for num in range(1, max_obj + 1):
        if num in offsets:
            buf.write(f"{offsets[num]:010d} 00000 n \n".encode("latin-1"))
        else:
            buf.write(b"0000000000 65535 f \n")
    buf.write(
        f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode("latin-1")
    )
    return buf.getvalue()


def make_encrypted_pdf(pages: list, password: str) -> bytes:
    """A real-password-protected PDF, built by encrypting make_pdf()'s output
    with pypdf's own writer — the same object make_pdf() was verified
    against, so pypdf.PdfReader(...).decrypt("") on the result is exercised
    against a real encryption, not a hand-rolled one."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(make_pdf(pages)))
    writer = PdfWriter()
    writer.append(reader)
    writer.encrypt(user_password=password, owner_password=password)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _no_files_left():
    assert list(imports.IMPORTS_DIR.glob("*")) == []


def _no_index_rows():
    idx = json.loads(imports._INDEX_PATH.read_text()) if imports._INDEX_PATH.exists() else {"imports": []}
    assert idx["imports"] == []


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


# --------------------------------------------------------------------------- #
# PDF upload
# --------------------------------------------------------------------------- #

def test_pdf_upload_saves_kind_and_page_count():
    saved = imports.save_import("statement.pdf", make_pdf(["Page one text", "Page two text"]))
    assert "error" not in saved
    assert saved["n"] == 1
    assert saved["name"] == "statement.pdf"
    assert saved["kind"] == "pdf"
    assert saved["pages"] == 2

    json_files = list(imports.IMPORTS_DIR.glob("*.json"))
    assert len(json_files) == 1

    listed = imports.list_imports()
    row = listed["imports"][0]
    assert row["kind"] == "pdf"
    assert row["pages"] == 2
    assert "rows" not in row
    assert "columns" not in row


# --------------------------------------------------------------------------- #
# PDF rejections — none leave a file or an index row behind
# --------------------------------------------------------------------------- #

def test_pdf_with_no_text_layer_rejected():
    out = imports.save_import("scanned.pdf", make_pdf(["", ""]))
    assert "error" in out
    assert "no text layer" in out["error"]
    _no_files_left()
    _no_index_rows()


def test_garbage_bytes_named_pdf_rejected():
    out = imports.save_import("fake.pdf", b"this is not a pdf at all, just garbage bytes")
    assert "error" in out
    assert "could not be read as a PDF" in out["error"]
    _no_files_left()
    _no_index_rows()


def test_password_protected_pdf_rejected():
    out = imports.save_import("secret.pdf", make_encrypted_pdf(["hidden text"], "hunter2"))
    assert "error" in out
    assert "password-protected" in out["error"]
    _no_files_left()
    _no_index_rows()


def test_txt_upload_still_rejected_alongside_pdf_support():
    out = imports.save_import("notes.txt", _csv_bytes("hello"))
    assert "error" in out
    assert "is not a .csv or .pdf file" in out["error"]
    _no_files_left()
    _no_index_rows()


def test_pdf_over_max_pages_rejected(monkeypatch):
    monkeypatch.setattr(imports, "MAX_PDF_PAGES", 3)
    out = imports.save_import("huge.pdf", make_pdf([f"page {i}" for i in range(5)]))
    assert "error" in out
    assert "limit is 3" in out["error"]
    _no_files_left()
    _no_index_rows()


# --------------------------------------------------------------------------- #
# read_import on a PDF
# --------------------------------------------------------------------------- #

def test_read_import_pdf_pages_under_budget_and_next_start_page():
    imports.save_import("doc.pdf", make_pdf(["one", "two", "three"]))
    page = imports.read_import(1)
    assert "error" not in page
    assert page["kind"] == "pdf"
    assert page["total_pages"] == 3
    assert page["start_page"] == 1
    assert [p["page"] for p in page["pages"]] == [1, 2, 3]
    assert page["next_start_page"] is None
    assert "end of file" in page["note"]


def test_read_import_pdf_paginates_with_small_budget(monkeypatch):
    monkeypatch.setattr(imports, "READ_CHAR_BUDGET", 5)
    imports.save_import("doc.pdf", make_pdf(["aaaa", "bbbb", "cccc"]))

    first = imports.read_import(1)
    assert "error" not in first
    assert "NOT the whole file" in first["note"]
    assert f"start_page={first['next_start_page']}" in first["note"]

    second = imports.read_import(1, start_page=first["next_start_page"])
    assert "error" not in second
    while second["next_start_page"] is not None:
        second = imports.read_import(1, start_page=second["next_start_page"])
    assert "end of file" in second["note"]


def test_read_import_pdf_start_page_out_of_range():
    imports.save_import("doc.pdf", make_pdf(["one", "two"]))
    assert "error" in imports.read_import(1, start_page=0)
    assert "error" in imports.read_import(1, start_page=3)


def test_read_import_pdf_start_page_two_returns_page_two_first():
    imports.save_import("doc.pdf", make_pdf(["page one text", "page two text"]))
    page = imports.read_import(1, start_page=2)
    assert "error" not in page
    assert page["pages"][0]["page"] == 2
    assert page["pages"][0]["text"] == "page two text"


def test_read_import_pdf_single_page_over_budget_is_cut(monkeypatch):
    monkeypatch.setattr(imports, "READ_CHAR_BUDGET", 10)
    long_text = "x" * 50
    imports.save_import("doc.pdf", make_pdf([long_text]))

    page = imports.read_import(1)
    assert "error" not in page
    assert page["pages"][0]["text"] == long_text[:10]
    assert "cut at 10 of 50 characters" in page["note"]


# --------------------------------------------------------------------------- #
# Pruning removes a dropped PDF's .json file
# --------------------------------------------------------------------------- #

def test_pruning_deletes_dropped_pdf_json(monkeypatch):
    monkeypatch.setattr(imports, "MAX_IMPORTS", 1)
    imports.save_import("first.pdf", make_pdf(["first page"]))
    json_files_before = list(imports.IMPORTS_DIR.glob("*.json"))
    assert len(json_files_before) == 1

    imports.save_import("second.csv", _csv_bytes("a\n1\n"))

    listed = imports.list_imports()
    assert listed["count"] == 1
    assert listed["imports"][0]["name"] == "second.csv"
    assert list(imports.IMPORTS_DIR.glob("*.json")) == []


# --------------------------------------------------------------------------- #
# An old index row with no "kind" (pre-PDF) still reads as CSV
# --------------------------------------------------------------------------- #

def test_old_index_row_with_no_kind_reads_as_csv():
    imports.IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    entry_id = "legacyentryid0000000000000000"
    csv_path = imports.IMPORTS_DIR / f"{entry_id}.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        f.write("a,b\n1,2\n")
    entry = {
        "id": entry_id,
        "name": "legacy.csv",
        "uploaded_at": "2026-01-01T00:00:00-05:00",
        "rows": 1,
        "columns": ["a", "b"],
        "bytes": csv_path.stat().st_size,
        # No "kind" key at all — this is the pre-PDF shape.
    }
    imports._save_index({"imports": [entry]})

    listed = imports.list_imports()
    assert listed["imports"][0]["kind"] == "csv"
    assert listed["imports"][0]["rows"] == 1

    page = imports.read_import(1)
    assert "error" not in page
    assert page["rows"] == [["1", "2"]]
