"""app.core.files: magic-byte sniffing (extensions are only hints), Office zip-bomb checks, PDF probes."""
from __future__ import annotations

import gzip
import io
import struct
import zipfile
from pathlib import Path

import numpy as np
import pymupdf
import pytest
from PIL import Image

from app.core import files as kinds
from app.core.files import FileKindError, sniff


def img_bytes(fmt: str, size=(32, 24), mode="RGB", **kw) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, (200, 30, 40) if mode == "RGB" else 128).save(buf, fmt, **kw)
    return buf.getvalue()


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def pdf_bytes(pages: int = 2, *, password: str | None = None) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    kw = {}
    if password:
        kw = {"encryption": pymupdf.PDF_ENCRYPT_AES_256, "user_pw": password, "owner_pw": password + "-owner"}
    data = doc.tobytes(**kw)
    doc.close()
    return data


def ooxml(kind: str = "docx", *, extra: dict[str, bytes] | None = None, pages: int | None = 3,
          compress: int = zipfile.ZIP_DEFLATED) -> bytes:
    folder = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml", "pptx": "ppt/presentation.xml"}[kind]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compress) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(folder, "<doc/>")
        if kind == "pptx":
            zf.writestr("ppt/slides/slide1.xml", "<s/>")
            zf.writestr("ppt/slides/slide2.xml", "<s/>")
        if pages is not None and kind == "docx":
            zf.writestr("docProps/app.xml", f"<Properties><Pages>{pages}</Pages></Properties>")
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def odf(mimetype: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
        zf.writestr("content.xml", "<office/>")
    return buf.getvalue()


# ------------------------------------------------------------------ images
@pytest.mark.parametrize(("fmt", "expected"), [("JPEG", "jpeg"), ("PNG", "png"), ("GIF", "gif"), ("WEBP", "webp"),
                                               ("BMP", "bmp"), ("TIFF", "tiff"), ("ICO", "ico")])
def test_images_by_magic(tmp_path, fmt, expected):
    found = sniff(write(tmp_path, "x.bin", img_bytes(fmt)), "x.bin")
    assert (found.kind, found.format) == ("image", expected)
    assert found.mime.startswith("image/")


def test_heic_and_avif_by_ftyp_brand(tmp_path):
    import pillow_heif

    pillow_heif.register_heif_opener()
    assert sniff(write(tmp_path, "a", img_bytes("HEIF", quality=50))).format == "heic"
    assert sniff(write(tmp_path, "b", img_bytes("AVIF", quality=50))).format == "avif"


def test_the_extension_never_decides_the_kind(tmp_path):
    png_named_pdf = sniff(write(tmp_path, "report.pdf", img_bytes("PNG")), "report.pdf")
    assert (png_named_pdf.kind, png_named_pdf.format) == ("image", "png")
    pdf_named_jpg = sniff(write(tmp_path, "photo.jpg", pdf_bytes()), "photo.jpg")
    assert pdf_named_jpg.kind == "pdf"


def test_psd_psb_svg_svgz_and_raw(tmp_path):
    psd = b"8BPS\x00\x01" + b"\x00" * 6 + struct.pack(">HIIHH", 3, 10, 10, 8, 3) + b"\x00" * 64
    assert sniff(write(tmp_path, "a.psd", psd)).format == "psd"
    psb = b"8BPS\x00\x02" + psd[6:]
    assert sniff(write(tmp_path, "a.psb", psb)).format == "psb"
    svg = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>'
    assert sniff(write(tmp_path, "a.svg", svg)) == kinds.Sniffed("image", "svg", "image/svg+xml")
    assert sniff(write(tmp_path, "a.svgz", gzip.compress(svg)), "a.svgz").format == "svg"
    raf = b"FUJIFILMCCD-RAW 0201FF383501" + b"\x00" * 100
    assert sniff(write(tmp_path, "a.raf", raf)).format == "raf"
    cr2 = b"II*\x00\x10\x00\x00\x00CR\x02\x00" + b"\x00" * 100
    assert sniff(write(tmp_path, "a.cr2", cr2)).format == "cr2"
    # a TIFF-structured RAW without its own mark is told apart by the extension hint
    assert sniff(write(tmp_path, "b.nef", img_bytes("TIFF")), "b.nef").format == "nef"


def test_xhtml_with_inline_svg_is_html_not_an_image(tmp_path):
    doc = b'<!DOCTYPE html><html><body><svg width="1" height="1"></svg></body></html>'
    assert sniff(write(tmp_path, "page.html", doc)).kind == "html"


# ------------------------------------------------------------------ PDF
def test_pdf_magic_may_follow_junk_but_postscript_is_refused(tmp_path):
    assert sniff(write(tmp_path, "a.pdf", b"\r\n  junk" + pdf_bytes())).kind == "pdf"
    ps = b"%!PS-Adobe-3.0\n%%Creator: x\n%PDF-1.4 hidden\n"
    assert sniff(write(tmp_path, "a.pdf", ps), "a.pdf").kind == "other"
    late = b"x" * 2000 + pdf_bytes()  # magic beyond the first KB: not a PDF
    assert sniff(write(tmp_path, "late.pdf", late), "late.pdf").kind == "other"


def test_pdf_probe_counts_pages_and_detects_passwords(tmp_path):
    info = kinds.document_info(write(tmp_path, "a.pdf", pdf_bytes(3)), sniff(tmp_path / "a.pdf"))
    assert info == {"kind": "pdf", "format": "pdf", "mime": "application/pdf", "pages": 3, "encrypted": False}
    locked = write(tmp_path, "b.pdf", pdf_bytes(2, password="s3cret"))
    info = kinds.document_info(locked, sniff(locked))
    assert info["encrypted"] is True and info["pages"] is None


def test_damaged_pdf_is_kept_for_the_repair_tool(tmp_path):
    broken = write(tmp_path, "c.pdf", b"%PDF-1.7\n1 0 obj << /Type /Catalog >>\n" + b"\x00garbage" * 50)
    info = kinds.document_info(broken, sniff(broken))
    assert info["kind"] == "pdf"
    assert info.get("damaged") is True or info["pages"] is not None  # MuPDF may repair tiny files itself


# ------------------------------------------------------------------ Office
@pytest.mark.parametrize("fmt", ["docx", "xlsx", "pptx"])
def test_ooxml_by_parts(tmp_path, fmt):
    found = sniff(write(tmp_path, "file.zip", ooxml(fmt)), "file.zip")
    assert (found.kind, found.format) == ("office", fmt)
    assert found.pages == {"docx": 3, "xlsx": None, "pptx": 2}[fmt]


@pytest.mark.parametrize(("mimetype", "fmt"), [("application/vnd.oasis.opendocument.text", "odt"),
                                               ("application/vnd.oasis.opendocument.spreadsheet", "ods"),
                                               ("application/vnd.oasis.opendocument.presentation", "odp")])
def test_odf_by_mimetype(tmp_path, mimetype, fmt):
    assert sniff(write(tmp_path, "x", odf(mimetype))).format == fmt


def test_rtf_txt_csv_and_plain_zip(tmp_path):
    assert sniff(write(tmp_path, "a.rtf", b"{\\rtf1\\ansi hello}")).format == "rtf"
    assert sniff(write(tmp_path, "a.txt", "héllo wörld\n".encode()), "a.txt").format == "txt"
    assert sniff(write(tmp_path, "a.csv", b"a,b\n1,2\n"), "a.csv").format == "csv"
    assert sniff(write(tmp_path, "a.bin", b"a,b\n1,2\n"), "a.bin").kind == "other"  # text needs its extension
    assert sniff(write(tmp_path, "b.txt", b"\x00\x01\x02binary"), "b.txt").kind == "other"
    plain = io.BytesIO()
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("readme", "x")
    assert sniff(write(tmp_path, "a.zip", plain.getvalue())).kind == "other"


def test_legacy_ole_and_encrypted_ooxml(tmp_path):
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600
    assert sniff(write(tmp_path, "a.xls", ole), "a.xls") == kinds.Sniffed("office", "xls", kinds.MIME["xls"])
    assert sniff(write(tmp_path, "a.doc", ole), "a.doc").format == "doc"
    encrypted = ole + "EncryptedPackage".encode("utf-16-le") + b"\x00" * 64
    found = sniff(write(tmp_path, "secret.docx", encrypted), "secret.docx")
    assert (found.kind, found.format, found.encrypted) == ("office", "docx", True)


def test_zip_bomb_by_ratio_is_refused_before_any_engine(tmp_path):
    bomb = ooxml("docx", extra={"word/media/blob.bin": b"\0" * (20 * 1024 * 1024)})  # ~1000:1
    with pytest.raises(FileKindError) as info:
        sniff(write(tmp_path, "bomb.docx", bomb), "bomb.docx")
    assert info.value.status == 422 and "zip bomb" in info.value.detail


def test_zip_bomb_by_total_size_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(kinds, "OFFICE_MAX_UNCOMPRESSED", 1024 * 1024)
    noise = np.random.default_rng(1).integers(0, 256, 2 * 1024 * 1024, dtype=np.uint8).tobytes()
    big = ooxml("docx", extra={"word/media/noise.bin": noise}, compress=zipfile.ZIP_STORED)
    with pytest.raises(FileKindError) as info:
        sniff(write(tmp_path, "big.docx", big), "big.docx")
    assert info.value.status == 413 and "expands to" in info.value.detail


def test_too_many_zip_entries_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(kinds, "OFFICE_MAX_ENTRIES", 10)
    many = ooxml("docx", extra={f"word/p{i}.xml": b"<p/>" for i in range(20)})
    with pytest.raises(FileKindError) as info:
        sniff(write(tmp_path, "many.docx", many), "many.docx")
    assert info.value.status == 422


def test_damaged_zip_is_415(tmp_path):
    with pytest.raises(FileKindError) as info:
        sniff(write(tmp_path, "x.docx", b"PK\x03\x04" + b"\x00" * 100), "x.docx")
    assert info.value.status == 415


# ------------------------------------------------------------------ HTML, certificates, the rest
def test_html_detection_and_size_cap(tmp_path, monkeypatch):
    assert sniff(write(tmp_path, "a", b"<!-- c -->\n<!DOCTYPE html><html><p>x")).kind == "html"
    assert sniff(write(tmp_path, "b.html", b"<div>fragment</div>"), "b.html").kind == "html"
    assert sniff(write(tmp_path, "c", b"<div>fragment</div>")).kind == "other"  # no html mark, no hint
    monkeypatch.setattr(kinds, "HTML_MAX_BYTES", 100)
    with pytest.raises(FileKindError) as info:
        sniff(write(tmp_path, "d.html", b"<html>" + b"x" * 200), "d.html")
    assert info.value.status == 413


def test_certificates_need_their_extension_and_a_der_header(tmp_path):
    der = b"\x30\x82\x01\x00" + b"\x01" * 256
    assert sniff(write(tmp_path, "id.p12", der), "id.p12").kind == "cert"
    assert sniff(write(tmp_path, "id.pfx", der), "id.pfx").format == "p12"
    assert sniff(write(tmp_path, "id.bin", der), "id.bin").kind == "other"
    assert sniff(write(tmp_path, "big.p12", der + b"\x00" * (70 * 1024)), "big.p12").kind == "other"


def test_unknown_bytes_are_other(tmp_path):
    found = sniff(write(tmp_path, "x.exe", b"MZ\x90\x00" + b"\x00" * 100), "x.exe")
    assert found == kinds.Sniffed("other", "exe", "application/octet-stream")
    assert "“.exe” files" in kinds.unsupported_detail("x.exe")
    assert kinds.unsupported_detail("noext") == "This file type can't be opened (unsupported or damaged file)"


