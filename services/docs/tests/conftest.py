"""heypdf-docs test fixtures: every sample is generated here (no binaries in git).

The suite runs INSIDE the heypdf-docs image (real LibreOffice / Ghostscript / OCRmyPDF / prlimit), e.g.
``docker build --target test -t heypdf-docs:test services/docs && docker run --rm --read-only --tmpfs /tmp
--network none --cap-drop ALL heypdf-docs:test``. Limits are lowered so the abuse cases stay fast.
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

TEST_ENV = {"HEYP_DOCS_TMP": "/tmp/test-jobs", "HEYP_DOCS_MAX_MB": "30", "HEYP_DOCS_ZIP_MAX_MB": "20",
            "HEYP_DOCS_MAX_PAGES": "50", "HEYP_DOCS_OCR_MAX_PAGES": "3", "HEYP_DOCS_PPTX_MAX_PAGES": "10",
            "HEYP_DOCS_TEXT_MAX_MB": "2", "HEYP_DOCS_QUEUE": "2"}
os.environ.update(TEST_ENV)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pikepdf  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import worker  # noqa: E402

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
PASSWD_MARKERS = ("root:x:0:0", "/bin/bash", "/usr/sbin/nologin", "daemon:x:")


@pytest.fixture(scope="session")
def client():
    with TestClient(worker.app) as c:
        yield c


def post(client, op: str, data: bytes, name: str = "file.pdf", options: dict | None = None,
         password: str | None = None):
    form = {"options": json.dumps(options or {})}
    if password is not None:
        form["password"] = password
    return client.post(f"/v1/{op}", files={"file": (name, data, "application/octet-stream")}, data=form)


def info(resp) -> dict:
    return json.loads(base64.urlsafe_b64decode(resp.headers["x-docs-info"]))


def pdf_text(data: bytes) -> str:
    from pdfminer.high_level import extract_text

    return extract_text(io.BytesIO(data))


def assert_clean_error(resp, status: int, code: str | None = None) -> dict:
    """Error bodies are short JSON; never engine output, paths or tracebacks."""
    assert resp.status_code == status, resp.text[:500]
    body = resp.json()
    assert set(body) <= {"error", "detail", "needs_password"}, body
    if code:
        assert body["error"] == code, body
    for leak in ("/tmp", "Traceback", "soffice", "GPL Ghostscript", "qpdf", "Error:", "File \""):
        assert leak not in body["detail"], body
    return body


# ----------------------------------------------------------------------------------------------- PDF samples
def make_text_pdf(pages: int = 3, text: str = "Hello heypdf") -> bytes:
    pdf = pikepdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                                                BaseFont=pikepdf.Name.Helvetica))
    for i in range(pages):
        page = pdf.add_blank_page(page_size=(595, 842))
        page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        page.obj.Contents = pdf.make_stream(f"BT /F1 24 Tf 72 720 Td ({text} page {i + 1}) Tj ET".encode())
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def make_form_pdf() -> bytes:
    pdf = pikepdf.open(io.BytesIO(make_text_pdf(1)))
    field = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Widget,
                                                 FT=pikepdf.Name.Tx, T=pikepdf.String("name"),
                                                 Rect=[72, 600, 300, 630], P=pdf.pages[0].obj))
    pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([field]))
    pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([field]))
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def make_scan_pdf(lines=("The quick brown fox", "jumps over the lazy dog", "heypdf OCR test 2026")) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("L", (1654, 2339), 255)  # A4 at 200 dpi
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT, 64)
    for i, line in enumerate(lines):
        draw.text((150, 250 + i * 120), line, fill=0, font=font)
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=200)
    return buf.getvalue()


def make_photo_pdf() -> bytes:
    """An image-heavy PDF (noise photo at 300 dpi) that Ghostscript can shrink."""
    import random

    from PIL import Image

    rnd = random.Random(1)
    img = Image.new("RGB", (1200, 900))
    img.putdata([(rnd.randrange(256), (x * 7) % 256, (x // 3) % 256) for x in range(1200 * 900)])
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=300, quality=95)
    return buf.getvalue()


def encrypt_pdf(data: bytes, user: str = "s3cret", owner: str = "owner-pw") -> bytes:
    pdf = pikepdf.open(io.BytesIO(data))
    buf = io.BytesIO()
    pdf.save(buf, encryption=pikepdf.Encryption(user=user, owner=owner, R=6))
    return buf.getvalue()


# -------------------------------------------------------------------------------------------- Office samples
W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
CT_DOCX = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
           'content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
           'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/'
           'document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.'
           'main+xml"/></Types>')
RELS_DOCX = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/'
             '2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
             '2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')


def make_docx(paragraphs=("Hello heypdf from Word",)) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CT_DOCX)
        z.writestr("_rels/.rels", RELS_DOCX)
        z.writestr("word/document.xml", f'<?xml version="1.0" encoding="UTF-8"?><w:document {W_NS}><w:body>'
                                        f"{body}</w:body></w:document>")
    return buf.getvalue()


def make_xlsx(rows=(("Item", "Qty"), ("Apples", "3"), ("Pears", "5"))) -> bytes:
    ct = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
          'content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
          'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/'
          'workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-'
          'officedocument.spreadsheetml.worksheet+xml"/></Types>')
    rels = RELS_DOCX.replace("word/document.xml", "xl/workbook.xml")
    wb = ('<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
          '2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
          '<sheet name="Fruit" sheetId="1" r:id="rId1"/></sheets></workbook>')
    wb_rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/'
               '2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
               '2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
    cells = "".join(
        f'<row r="{r + 1}">' + "".join(f'<c r="{"AB"[c]}{r + 1}" t="inlineStr"><is><t>{v}</t></is></c>'
                                        for c, v in enumerate(row)) + "</row>" for r, row in enumerate(rows))
    sheet = ('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/'
             f'spreadsheetml/2006/main"><sheetData>{cells}</sheetData></worksheet>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in (("[Content_Types].xml", ct), ("_rels/.rels", rels), ("xl/workbook.xml", wb),
                           ("xl/_rels/workbook.xml.rels", wb_rels), ("xl/worksheets/sheet1.xml", sheet)):
            z.writestr(name, text)
    return buf.getvalue()


ODT_NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
          'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:xlink="http://www.w3.org/1999/xlink" '
          'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
          'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"')


def make_odt(body: str = "<text:p>Hello heypdf from Writer</text:p>", manifest_extra: str = "") -> bytes:
    manifest = ('<?xml version="1.0" encoding="UTF-8"?><manifest:manifest xmlns:manifest="urn:oasis:names:tc:'
                'opendocument:xmlns:manifest:1.0" manifest:version="1.3"><manifest:file-entry manifest:full-path="/" '
                'manifest:media-type="application/vnd.oasis.opendocument.text"/><manifest:file-entry '
                f'manifest:full-path="content.xml" manifest:media-type="text/xml">{manifest_extra}'
                '</manifest:file-entry></manifest:manifest>')
    content = (f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {ODT_NS} office:version="1.3">'
               f"<office:body><office:text>{body}</office:text></office:body></office:document-content>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/vnd.oasis.opendocument.text")  # stored, first
        z.writestr("META-INF/manifest.xml", manifest)
        z.writestr("content.xml", content)
    return buf.getvalue()


def make_zip_bomb_docx(path: Path, mb: int) -> Path:
    """The critic's DOCX bomb: a tiny file whose document.xml inflates to ``mb`` MB."""
    para = "<w:p><w:r><w:t>" + "lorem ipsum dolor sit amet " * 8 + "</w:t></w:r></w:p>"
    n = mb * (1 << 20) // len(para)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("[Content_Types].xml", CT_DOCX)
        z.writestr("_rels/.rels", RELS_DOCX)
        with z.open("word/document.xml", "w", force_zip64=True) as f:
            f.write(f'<?xml version="1.0" encoding="UTF-8"?><w:document {W_NS}><w:body>'.encode())
            p = para.encode()
            for _ in range(n):
                f.write(p)
            f.write(b"</w:body></w:document>")
    return path


def run_helper(*args: str, password: str = "") -> dict:
    r = subprocess.run([sys.executable, "-I", str(worker.HELPER), *args], input=password.encode(),
                       capture_output=True, timeout=120)
    return json.loads(r.stdout.decode().strip().splitlines()[-1])


def encrypt_docx(data: bytes, tmp_path: Path, password: str = "s3cret") -> bytes:
    src, dst = tmp_path / "plain.docx", tmp_path / "enc.docx"
    src.write_bytes(data)
    assert run_helper("office-encrypt", str(src), str(dst), password=password) == {"ok": True}
    return dst.read_bytes()
