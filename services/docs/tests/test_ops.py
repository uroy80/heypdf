"""Happy paths and edge cases of every heypdf-docs operation, against the real engines."""
from __future__ import annotations

import io
import random

import pikepdf
import pytest
from conftest import (assert_clean_error, encrypt_docx, encrypt_pdf, info, make_docx, make_form_pdf, make_odt,
                      make_photo_pdf, make_scan_pdf, make_text_pdf, make_xlsx, pdf_text, post)


def pages(data: bytes) -> int:
    with pikepdf.open(io.BytesIO(data)) as pdf:
        return len(pdf.pages)


# ------------------------------------------------------------------------------------------------- service
def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_capabilities(client):
    caps = client.get("/v1/capabilities").json()
    assert set(caps["ops"]) == {"office_to_pdf", "pdf_to_pptx", "ocr", "compress", "pdfa", "repair"}
    assert {"eng", "hin", "tam", "fra", "chi_sim"} <= set(caps["ocr_languages"]) and "osd" not in caps["ocr_languages"]
    assert caps["ops"]["compress"]["options"]["properties"]["level"]["default"] == "recommended"
    assert "html" not in caps["office_inputs"] and "htm" not in caps["office_inputs"]
    assert all(v != "unknown" for v in caps["versions"].values()), caps["versions"]


def test_unknown_op(client):
    assert_clean_error(post(client, "exec", b"%PDF-1.4"), 404, "unknown_op")


# ----------------------------------------------------------------------------------------- office_to_pdf
@pytest.mark.parametrize("name,data,needle", [
    ("letter.docx", make_docx(), "Hello heypdf from Word"),
    ("letter.odt", make_odt(), "Hello heypdf from Writer"),
    ("letter.rtf", rb"{\rtf1\ansi Hello heypdf from RTF\par}", "Hello heypdf from RTF"),
    ("notes.txt", "Hello heypdf from text — café\n".encode(), "Hello heypdf from text"),
    ("notes.txt", "Hello heypdf in UTF-16".encode("utf-16"), "Hello heypdf in UTF-16"),
    ("legacy.txt", "Hello heypdf in cp1252 caf\xe9".encode("cp1252"), "Hello heypdf in cp1252"),
    ("table.csv", b"Item;Qty\nApples;3\nPears;5\n", "Apples"),
    ("table.xlsx", make_xlsx(), "Pears"),
])
def test_office_to_pdf(client, name, data, needle):
    r = post(client, "office_to_pdf", data, name)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf" and r.content.startswith(b"%PDF-")
    meta = info(r)
    assert meta["pages_out"] >= 1 and meta["op"] == "office_to_pdf" and meta["warnings"] == []
    assert needle in pdf_text(r.content)


def test_office_csv_uses_the_detected_separator(client):
    r = post(client, "office_to_pdf", b"a;b;c\n1;2;3\n", "t.csv")
    text = pdf_text(r.content)
    assert "a;b;c" not in text and "a" in text


def test_office_content_beats_extension(client):
    """A .doc that is really a DOCX converts (magic bytes decide the kind, the extension is a hint)."""
    r = post(client, "office_to_pdf", make_docx(), "misnamed.doc")
    assert r.status_code == 200 and "Hello heypdf" in pdf_text(r.content)


def test_office_pdfa_and_single_page_sheets(client):
    r = post(client, "office_to_pdf", make_docx(), "a.docx", {"pdfa": True})
    assert r.status_code == 200
    with pikepdf.open(io.BytesIO(r.content)) as pdf, pdf.open_metadata() as meta:
        assert meta.get("pdfaid:part") == "2"
    rows = [("Row", str(i)) for i in range(400)]  # several printed pages normally
    multi = post(client, "office_to_pdf", make_xlsx(rows), "long.xlsx")
    single = post(client, "office_to_pdf", make_xlsx(rows), "long.xlsx", {"one_page_per_sheet": True})
    assert pages(multi.content) > 1 and pages(single.content) == 1


def test_office_encrypted_docx(client, tmp_path):
    # msoffcrypto's (test-only) encryptor mis-writes packages under 4 KB (compressed), so pad the document with
    # text that does not compress well; real Office files are far bigger than that anyway.
    rnd = random.Random(7)
    filler = [f"Paragraph {i} " + "".join(rnd.choice("abcdefghijklmnopqrstuvwxyz ") for _ in range(80))
              for i in range(200)]
    enc = encrypt_docx(make_docx(["Top secret plan", *filler]), tmp_path, "s3cret")
    body = assert_clean_error(post(client, "office_to_pdf", enc, "enc.docx"), 422, "needs_password")
    assert body["needs_password"] is True
    body = assert_clean_error(post(client, "office_to_pdf", enc, "enc.docx", password="nope"), 422, "wrong_password")
    assert body["needs_password"] is True
    r = post(client, "office_to_pdf", enc, "enc.docx", password="s3cret")
    assert r.status_code == 200 and "Top secret plan" in pdf_text(r.content)
    assert any("not password-protected" in w for w in info(r)["warnings"])


def test_office_encrypted_odf_is_refused_clearly(client):
    enc = make_odt(manifest_extra='<manifest:encryption-data manifest:checksum-type="SHA1/1K" manifest:checksum="x"/>')
    body = assert_clean_error(post(client, "office_to_pdf", enc, "x.odt", password="pw"), 422, "encrypted_unsupported")
    assert "needs_password" not in body


@pytest.mark.parametrize("name,data", [
    ("page.html", b"<html><body>hi</body></html>"),
    ("page.docx", b"<!doctype html><html><body><img src='file:///etc/passwd'></body></html>"),
    ("tool.exe", b"MZ\x90\x00"),
    ("noext", make_docx()),
    ("archive.docx", b"PK\x03\x04" + b"\x00" * 100),
    ("binary.txt", b"\x00\x01\x02" * 100),
    ("scan.pdf", make_text_pdf(1)),
])
def test_office_rejects_non_office_input(client, name, data):
    r = post(client, "office_to_pdf", data, name)
    assert r.status_code in (415, 422), r.text
    assert_clean_error(r, r.status_code)


def test_office_plain_zip_is_not_a_document(client):
    buf = io.BytesIO()
    import zipfile
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "hi")
    assert_clean_error(post(client, "office_to_pdf", buf.getvalue(), "x.docx"), 415, "unsupported_type")


def test_office_html_in_txt_is_rendered_as_text(client):
    r = post(client, "office_to_pdf", b"<html><body><h1>Title</h1><img src='http://example.com/x.png'></body></html>",
             "markup.txt")
    assert r.status_code == 200 and "<h1>Title</h1>" in pdf_text(r.content)


# ----------------------------------------------------------------------------------------------- pdf_to_pptx
def test_pdf_to_pptx_and_back(client):
    r = post(client, "pdf_to_pptx", make_text_pdf(2, "Slide text"))
    assert r.status_code == 200, r.text
    assert r.content[:4] == b"PK\x03\x04" and "presentationml" in r.headers["content-type"]
    back = post(client, "office_to_pdf", r.content, "deck.pptx")
    assert back.status_code == 200 and pages(back.content) == 2 and "Slide text" in pdf_text(back.content)


def test_pdf_to_pptx_page_cap(client):
    assert_clean_error(post(client, "pdf_to_pptx", make_text_pdf(11)), 413, "too_many_pages")


# ------------------------------------------------------------------------------------------------------- OCR
def test_ocr_makes_scans_searchable(client):
    scan = make_scan_pdf()
    assert pdf_text(scan).strip() == ""
    r = post(client, "ocr", scan, options={"languages": ["eng"]})
    assert r.status_code == 200, r.text
    text = pdf_text(r.content).lower()
    assert "quick brown fox" in text and "lazy dog" in text
    assert info(r)["languages"] == ["eng"] and info(r)["pages_out"] == 1


def test_ocr_skips_pages_that_already_have_text(client):
    r = post(client, "ocr", make_text_pdf(1, "Born digital"), options={"languages": ["eng", "hin"]})
    assert r.status_code == 200 and "Born digital" in pdf_text(r.content)


def test_ocr_pdfa_output(client):
    r = post(client, "ocr", make_scan_pdf(), options={"pdfa": True, "deskew": False})
    assert r.status_code == 200, r.text
    with pikepdf.open(io.BytesIO(r.content)) as pdf, pdf.open_metadata() as meta:
        assert meta.get("pdfaid:part") == "2"


@pytest.mark.parametrize("opts,status", [
    ({"languages": ["xyz"]}, 400), ({"languages": ["osd"]}, 400), ({"languages": ["en; rm -rf /"]}, 400),
    ({"languages": []}, 400), ({"languages": ["eng", "hin", "tam", "fra", "deu"]}, 400),
    ({"mode": "strip"}, 400), ({"unknown": 1}, 400),
])
def test_ocr_bad_options(client, opts, status):
    assert_clean_error(post(client, "ocr", make_scan_pdf(), options=opts), status, "bad_options")


def test_ocr_page_cap(client):
    assert_clean_error(post(client, "ocr", make_text_pdf(4)), 413, "too_many_pages")


# --------------------------------------------------------------------------------------------------- compress
@pytest.mark.parametrize("level", ["extreme", "recommended", "less"])
def test_compress_levels_shrink_images(client, level):
    src = make_photo_pdf()
    r = post(client, "compress", src, options={"level": level})
    assert r.status_code == 200, r.text
    assert len(r.content) < len(src) and pages(r.content) == 1
    assert info(r)["level"] == level and not info(r).get("kept_original")


def test_compress_ordering(client):
    src = make_photo_pdf()
    sizes = [len(post(client, "compress", src, options={"level": lv}).content) for lv in ("extreme", "recommended", "less")]
    assert sizes[0] < sizes[1] < sizes[2]


def test_compress_never_returns_a_bigger_file(client):
    src = make_text_pdf(1)
    r = post(client, "compress", src)
    assert r.status_code == 200 and len(r.content) <= len(src)
    assert "Hello heypdf" in pdf_text(r.content)


def test_compress_warns_about_forms(client):
    r = post(client, "compress", make_form_pdf())
    assert r.status_code == 200 and any("form fields" in w.lower() for w in info(r)["warnings"])


def test_compress_encrypted_pdf(client):
    enc = encrypt_pdf(make_photo_pdf())
    assert assert_clean_error(post(client, "compress", enc), 422, "needs_password")["needs_password"] is True
    assert_clean_error(post(client, "compress", enc, password="wrong"), 422, "wrong_password")
    r = post(client, "compress", enc, password="s3cret")
    assert r.status_code == 200
    with pikepdf.open(io.BytesIO(r.content)) as pdf:
        assert not pdf.is_encrypted
    assert any("password" in w for w in info(r)["warnings"])


def test_compress_owner_password_only(client):
    enc = encrypt_pdf(make_photo_pdf(), user="")
    r = post(client, "compress", enc)
    assert r.status_code == 200 and any("restrictions" in w for w in info(r)["warnings"])


@pytest.mark.parametrize("opts", [{"level": "max"}, {"level": 3}, {"dpi": 10}])
def test_compress_bad_options(client, opts):
    assert_clean_error(post(client, "compress", make_text_pdf(1), options=opts), 400, "bad_options")


def test_options_must_be_a_json_object(client):
    r = client.post("/v1/compress", files={"file": ("a.pdf", make_text_pdf(1))}, data={"options": "[1,2]"})
    assert_clean_error(r, 400, "bad_options")
    r = client.post("/v1/compress", files={"file": ("a.pdf", make_text_pdf(1))}, data={"options": "{nope"})
    assert_clean_error(r, 400, "bad_options")


# ------------------------------------------------------------------------------------------------------ PDF/A
@pytest.mark.parametrize("conformance", ["1b", "2b", "3b"])
def test_pdfa(client, conformance):
    r = post(client, "pdfa", make_text_pdf(2), options={"conformance": conformance})
    assert r.status_code == 200, r.text
    with pikepdf.open(io.BytesIO(r.content)) as pdf, pdf.open_metadata() as meta:
        assert meta.get("pdfaid:part") == conformance[0] and meta.get("pdfaid:conformance").lower() == "b"
        assert len(pdf.pages) == 2
    assert info(r)["conformance"] == conformance


def test_pdfa_bad_conformance(client):
    assert_clean_error(post(client, "pdfa", make_text_pdf(1), options={"conformance": "2u"}), 400, "bad_options")


# ----------------------------------------------------------------------------------------------------- repair
def _broken_tail(data: bytes) -> bytes:
    return data[: int(len(data) * 0.97)]


def _broken_xref(data: bytes) -> bytes:
    i = data.rfind(b"startxref")
    return data[:i] + b"startxref\n12345\n%%EOF\n"


@pytest.mark.parametrize("breaker", [_broken_tail, _broken_xref])
def test_repair_recovers_damaged_files(client, breaker):
    src = make_text_pdf(3)
    pdf = pikepdf.open(io.BytesIO(src))
    buf = io.BytesIO()
    pdf.save(buf, object_stream_mode=pikepdf.ObjectStreamMode.disable)
    r = post(client, "repair", breaker(buf.getvalue()))
    assert r.status_code == 200, r.text
    assert pages(r.content) == 3 and "Hello heypdf page 3" in pdf_text(r.content)


def test_repair_corrupt_content_stream_falls_back_to_ghostscript(client):
    src = make_text_pdf(2)
    with pikepdf.open(io.BytesIO(src)) as pdf:
        pdf.pages[0].obj.Contents = pdf.make_stream(b"x\x9c" + b"\xff" * 40)  # broken deflate data
        pdf.pages[0].obj.Contents.Filter = pikepdf.Name.FlateDecode
        buf = io.BytesIO()
        pdf.save(buf)
    r = post(client, "repair", buf.getvalue())
    assert r.status_code == 200, r.text
    assert info(r)["engine"] == "ghostscript" and pages(r.content) >= 1


def test_repair_junk_before_header(client):
    r = post(client, "repair", b"garbage-from-an-email-client\r\n" + make_text_pdf(1))
    assert r.status_code == 200 and r.content.startswith(b"%PDF-")


def test_repair_hopeless_file(client):
    assert_clean_error(post(client, "repair", b"%PDF-1.7\n" + b"\x00garbage" * 200), 422, "not_repairable")


def test_repair_not_a_pdf(client):
    assert_clean_error(post(client, "repair", b"just text, no header here" * 10), 415, "unsupported_type")


# ------------------------------------------------------------------------------------------------- requests
def test_missing_file_part(client):
    assert_clean_error(client.post("/v1/compress", data={"options": "{}"}), 400, "bad_request")


def test_empty_file(client):
    assert_clean_error(post(client, "compress", b""), 400, "empty_file")


def test_upload_limit(client):
    big = b"%PDF-1.4\n" + b"0" * (31 * 1024 * 1024)
    assert_clean_error(post(client, "compress", big), 413, "too_large")


def test_job_directories_are_removed(client):
    import os

    post(client, "compress", make_text_pdf(1))
    post(client, "office_to_pdf", b"\x00", "x.docx")
    assert os.listdir("/tmp/test-jobs") == []
