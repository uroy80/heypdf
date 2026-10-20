"""Output fidelity regressions: file-name fields and the user's file name in Office → PDF,
complex-script OCR text layers, automatic page rotation, and repaired files that must read back clean."""
from __future__ import annotations

import io
import logging
import time
import zipfile

import pikepdf
import pytest
from conftest import CT_DOCX, FONT, RELS_DOCX, W_NS, info, make_odt, make_text_pdf, pdf_text, post

import helper
import worker

NOTO = "/usr/share/fonts/truetype/noto/"
LEAKS = ("/tmp", "jobs", "office_to_pdf", "input.bin", "normalized", "document.")


def assert_no_server_names(text: str) -> None:
    for leak in LEAKS:
        assert leak not in text, (leak, text)


# ------------------------------------------------------------------------- file-name fields
def make_docx_raw(body: str, header: str | None = None) -> bytes:
    """A DOCX whose document.xml body (and optional default header) is given as raw WordprocessingML."""
    ct, rels, sect = CT_DOCX, "", ""
    if header is not None:
        ct = ct.replace("</Types>", '<Override PartName="/word/header1.xml" ContentType="application/vnd.'
                        'openxmlformats-officedocument.wordprocessingml.header+xml"/></Types>')
        rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/'
                'package/2006/relationships"><Relationship Id="rIdH" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/header" Target="header1.xml"/></Relationships>')
        sect = '<w:sectPr><w:headerReference w:type="default" r:id="rIdH"/></w:sectPr>'
    r_ns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", RELS_DOCX)
        z.writestr("word/document.xml", f'<?xml version="1.0" encoding="UTF-8"?><w:document {W_NS} {r_ns}>'
                                        f"<w:body>{body}{sect}</w:body></w:document>")
        if header is not None:
            z.writestr("word/_rels/document.xml.rels", rels)
            z.writestr("word/header1.xml", f'<?xml version="1.0" encoding="UTF-8"?><w:hdr {W_NS}>{header}</w:hdr>')
    return buf.getvalue()


SIMPLE_FILENAME = ('<w:p><w:fldSimple w:instr=" FILENAME \\p \\* MERGEFORMAT "><w:r><w:t>Q3-simple.docx</w:t></w:r>'
                   "</w:fldSimple></w:p>")
COMPLEX_FILENAME = ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> FILE'
                    '</w:instrText></w:r><w:r><w:instrText xml:space="preserve">NAME \\p </w:instrText></w:r><w:r>'
                    '<w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>Q3-complex.docx</w:t></w:r><w:r>'
                    '<w:fldChar w:fldCharType="end"/></w:r></w:p>')
PAGE_FIELD = ('<w:p><w:r><w:t xml:space="preserve">Page </w:t></w:r><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
              '<w:r><w:instrText> PAGE </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r>'
              '<w:t>9</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')


def test_odf_file_name_fields_keep_their_saved_text(client):
    """An ODT file-name field rendered /tmp/jobs/office_to_pdf-<token>/document.odt into the PDF."""
    body = ('<text:p>Full: <text:file-name text:display="full">C:\\Reports\\Q3-report.odt</text:file-name></text:p>'
            '<text:p>Path: <text:file-name text:display="path">C:\\Reports</text:file-name></text:p>'
            '<text:p>Name: <text:file-name text:display="name-and-extension" text:fixed="false">Q3-report.odt'
            "</text:file-name></text:p>"
            '<text:p>Empty: <text:file-name text:display="full"/>.</text:p>')
    r = post(client, "office_to_pdf", make_odt(body), "upload.odt")
    assert r.status_code == 200, r.text
    text = pdf_text(r.content)
    assert "C:\\Reports\\Q3-report.odt" in text and "Path: C:\\Reports" in text and "Name: Q3-report.odt" in text
    assert_no_server_names(text)
    assert "upload" not in text and info(r)["frozen_fields"] == 4


def test_word_filename_fields_keep_their_saved_text(client):
    """DOCX FILENAME (\\p) printed the worker's job path or "document.docx" instead of the saved file name."""
    docx = make_docx_raw(SIMPLE_FILENAME + COMPLEX_FILENAME + PAGE_FIELD,
                         header='<w:p><w:fldSimple w:instr="FILENAME"><w:r><w:t>Header-Q3.docx</w:t></w:r>'
                                "</w:fldSimple></w:p>")
    r = post(client, "office_to_pdf", docx, "upload.docx")
    assert r.status_code == 200, r.text
    text = pdf_text(r.content)
    for saved in ("Q3-simple.docx", "Q3-complex.docx", "Header-Q3.docx"):
        assert saved in text, text
    assert_no_server_names(text)
    assert "upload" not in text and "Page 1" in text  # other fields still evaluate (PAGE: 1, not the saved 9)
    assert info(r)["frozen_fields"] == 3


def test_documents_without_file_name_fields_are_passed_through_untouched(tmp_path):
    src = tmp_path / "a.docx"
    src.write_bytes(make_docx_raw(PAGE_FIELD))
    dst = tmp_path / "out.bin"
    res = helper.cmd_office(str(src), "docx", str(dst), str(20 << 20), "100", "1000", str(2 << 20))
    assert "normalized" not in res and not dst.exists()


def test_frozen_package_keeps_every_entry_in_order(tmp_path):
    src = tmp_path / "a.odt"
    src.write_bytes(make_odt('<text:p><text:file-name text:display="full">x.odt</text:file-name></text:p>'))
    dst = tmp_path / "out.bin"
    res = helper.cmd_office(str(src), "odt", str(dst), str(20 << 20), "100", "1000", str(2 << 20))
    assert res["normalized"] is True and res["frozen_fields"] == 1
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(dst) as b:
        assert a.namelist() == b.namelist() and b.infolist()[0].compress_type == zipfile.ZIP_STORED
        assert b.read("mimetype") == a.read("mimetype")
        assert b'text:fixed="true"' in b.read("content.xml")


@pytest.mark.parametrize("xml,expect", [
    # another prefix for the WordprocessingML namespace, entities in the instruction, a self-closing field
    (b'<x:document xmlns:x="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><x:p><x:fldSimple '
     b'x:instr="FILENAME&#32;\\p"><x:r><x:t>Saved.docx</x:t></x:r></x:fldSimple><x:fldSimple x:instr="filename"/>'
     b"</x:p></x:document>",
     b'<x:document xmlns:x="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><x:p><x:r><x:t>Saved.docx'
     b"</x:t></x:r></x:p></x:document>"),
    # a FILENAME nested inside an IF field's instruction: only the inner field is unwrapped
    (b'<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:r><w:fldChar '
     b'w:fldCharType="begin"/></w:r><w:r><w:instrText>IF </w:instrText></w:r><w:r><w:fldChar w:fldCharType="begin"/>'
     b"</w:r><w:r><w:instrText>FILENAME</w:instrText></w:r><w:r><w:fldChar w:fldCharType=\"separate\"/></w:r><w:r>"
     b'<w:t>a.docx</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r><w:r><w:fldChar w:fldCharType="separate"/>'
     b'</w:r><w:r><w:t>yes</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>',
     b'<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:r><w:fldChar '
     b'w:fldCharType="begin"/></w:r><w:r><w:instrText>IF </w:instrText></w:r><w:r></w:r><w:r></w:r><w:r></w:r><w:r>'
     b'<w:t>a.docx</w:t></w:r><w:r></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>yes</w:t></w:r>'
     b'<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'),
])
def test_unwrap_word_filename_fields(xml, expect):
    out, n = helper.unwrap_word_filename_fields(xml)
    assert out == expect and n >= 1


def test_unwrap_leaves_other_fields_and_other_namespaces_alone():
    other = (b'<w:p xmlns:w="urn:not-word"><w:fldSimple w:instr="FILENAME"><w:r><w:t>x</w:t></w:r></w:fldSimple>'
             b"</w:p>")
    assert helper.unwrap_word_filename_fields(other) == (other, 0)
    page = (b'<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">' + PAGE_FIELD.encode()
            + b"</w:p>")
    assert helper.unwrap_word_filename_fields(page) == (page, 0)


def test_freeze_odf_fields_any_prefix():
    xml = (b'<o:x xmlns:o="o" xmlns:t="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><t:p><t:file-name '
           b"t:fixed='false' t:display=\"full\">a.odt</t:file-name><t:template-name/></t:p></o:x>")
    out, n = helper.freeze_odf_fields(xml)
    assert n == 2 and b"fixed='false'" not in out
    assert b'<t:file-name t:display="name-and-extension" t:fixed="true">a.odt' in out
    assert b'<t:template-name t:fixed="true"/>' in out


ODF_NS = " ".join(f'xmlns:{p}="urn:oasis:names:tc:opendocument:xmlns:{u}"' for p, u in (
    ("office", "office:1.0"), ("style", "style:1.0"), ("text", "text:1.0"), ("table", "table:1.0"),
    ("fo", "xsl-fo-compatible:1.0")))
ODF_HEADER = ('<office:automatic-styles><style:page-layout style:name="pm1"><style:page-layout-properties/>'
              '<style:header-style><style:header-footer-properties fo:min-height="0.75cm"/></style:header-style>'
              '</style:page-layout></office:automatic-styles><office:master-styles><style:master-page '
              'style:name="{master}" style:page-layout-name="pm1"><style:header>{header}</style:header>'
              "</style:master-page></office:master-styles>")


def make_odf(kind: str, body: str, header: str, master: str) -> bytes:
    """An ODF package with a page header (styles.xml master page) holding ``header``."""
    mt = f"application/vnd.oasis.opendocument.{kind}"
    manifest = ('<?xml version="1.0" encoding="UTF-8"?><manifest:manifest xmlns:manifest="urn:oasis:names:tc:'
                'opendocument:xmlns:manifest:1.0" manifest:version="1.3"><manifest:file-entry manifest:full-path="/" '
                f'manifest:media-type="{mt}"/><manifest:file-entry manifest:full-path="content.xml" '
                'manifest:media-type="text/xml"/><manifest:file-entry manifest:full-path="styles.xml" '
                'manifest:media-type="text/xml"/></manifest:manifest>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), mt)
        z.writestr("META-INF/manifest.xml", manifest)
        z.writestr("content.xml", f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {ODF_NS} '
                                  f'office:version="1.3"><office:body>{body}</office:body></office:document-content>')
        z.writestr("styles.xml", f'<?xml version="1.0" encoding="UTF-8"?><office:document-styles {ODF_NS} '
                                 f'office:version="1.3">{ODF_HEADER.format(master=master, header=header)}'
                                 "</office:document-styles>")
    return buf.getvalue()


def test_page_header_file_name_fields_never_print_the_job_path(client):
    """Writer headers keep the saved text; Calc's header fields ignore "fixed" and print the user's file name."""
    full = '<text:file-name text:display="full">C:\\Reports\\Q3.odt</text:file-name>'
    odt = make_odf("text", '<office:text><text:p text:style-name="Standard">body</text:p></office:text>',
                   f"<text:p>HDR {full}</text:p>", "Standard")
    r = post(client, "office_to_pdf", odt, "Mein Bericht.odt")
    assert r.status_code == 200, r.text
    text = pdf_text(r.content)
    assert "HDR C:\\Reports\\Q3.odt" in text and "body" in text
    assert_no_server_names(text)
    ods = make_odf("spreadsheet", '<office:spreadsheet><table:table table:name="Data"><table:table-row>'
                   "<table:table-cell><text:p>cell</text:p></table:table-cell></table:table-row></table:table>"
                   "</office:spreadsheet>",
                   f"<style:region-left><text:p>HDR {full.replace('Q3.odt', 'Q3.ods')}</text:p></style:region-left>",
                   "Default")
    r = post(client, "office_to_pdf", ods, "Mein Blatt.ods")
    assert r.status_code == 200, r.text
    text = pdf_text(r.content)
    assert "HDR Mein Blatt.ods" in text and "cell" in text
    assert_no_server_names(text)


def test_excel_header_folder_code_is_dropped(client):
    """&Z (the file's folder) printed file:///tmp/jobs/…; &F (the file name) shows the user's own name."""
    from conftest import make_xlsx

    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(make_xlsx()), "r") as zin, zipfile.ZipFile(buf, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item)
            if item.filename == "xl/worksheets/sheet1.xml":
                data = data.replace(b"</sheetData>", b"</sheetData><headerFooter><oddHeader>&amp;LDIR[&amp;Z] "
                                                     b"FILE[&amp;F] AND[&amp;&amp;Z]</oddHeader></headerFooter>")
            zout.writestr(item, data)
    r = post(client, "office_to_pdf", buf.getvalue(), "Mein Blatt.xlsx")
    assert r.status_code == 200, r.text
    text = pdf_text(r.content)
    assert "DIR[] FILE[Mein Blatt.xlsx] AND[&Z]" in text and "Pears" in text
    assert_no_server_names(text.replace("AND[&Z]", ""))
    assert info(r)["frozen_fields"] == 1


@pytest.mark.parametrize("rtf", [
    rb"{\rtf1\ansi Before {\field{\*\fldinst FILENAME \\p}{\fldrslt Saved.rtf}} after\par}",
    rb"{\rtf1\ansi Before {\field{\*\fldinst{\b  FILE}{NAME \\p}}{\fldrslt {\b Saved.rtf}}} after\par}",
    rb"{\rtf1\ansi Before {\field{\*\fldinst \'46ILENAME}{\fldrslt Saved.rtf}} after\par}",
])
def test_rtf_filename_fields_keep_their_saved_text(client, rtf):
    r = post(client, "office_to_pdf", rtf + rb"{\field{\*\fldinst PAGE}{\fldrslt 9}}", "Mein Text.rtf")
    assert r.status_code == 200, r.text
    text = pdf_text(r.content)
    assert "Before Saved.rtf after" in text, text
    assert_no_server_names(text)
    assert info(r)["frozen_fields"] == 1


def test_rtf_instruction_reader():
    data = rb"{\*\fldinst{\b  FILE}\u78\'3fAME \\p}{\fldrslt x}"
    assert helper._rtf_field_instruction(data, data.index(b"fldinst") + 7).split()[0] == "FILENAME"


# ----------------------------------------------------------------------- the user's file name
@pytest.mark.parametrize("name,shown", [("sales_q3.csv", "sales_q3"), ("Umsätze 2026.csv", "Umsätze 2026"),
                                        ("../../x%#.csv", "x_")])
def test_csv_page_header_shows_the_users_file_name(client, name, shown):
    """Calc prints a CSV's sheet name (= its file name) as page header: it was the internal 'document'."""
    r = post(client, "office_to_pdf", b"region,units\nnorth,12\nsouth,9\n", name)
    assert r.status_code == 200, r.text
    text = pdf_text(r.content)
    assert shown in text and "north" in text
    assert "document" not in text and "/" not in text


@pytest.mark.parametrize("name", ["normalized.txt", "input.txt", "out.txt", "lo-profile.txt", "src.txt"])
def test_user_file_names_never_clash_with_job_files(client, name):
    r = post(client, "office_to_pdf", "UTF-16 text named like a job file".encode("utf-16"), name)
    assert r.status_code == 200, r.text
    assert "UTF-16 text named like a job file" in pdf_text(r.content)


@pytest.mark.parametrize("filename,stem", [
    ("report.docx", "report"), ("Q3 report.final.docx", "Q3 report.final"), ("../../etc/passwd.docx", "passwd"),
    ("..\\..\\win.docx", "win"), ("a%2e#b?.csv", "a_2e_b_"), ("\x00\n.docx", "document"), ("", "document"),
    (None, "document"), ("...", "document"), ("विशाल रिपोर्ट.docx", "विशाल रिपोर्ट"), ("x" * 300 + ".docx", "x" * 60),
    ("ठ" * 60 + ".docx", "ठ" * 53),  # ≤ 160 UTF-8 bytes
])
def test_safe_stem(filename, stem):
    assert worker.safe_stem(filename) == stem


def test_logs_never_carry_the_users_file_name(caplog):
    job = worker.Job("office_to_pdf", time.monotonic() + 60)
    try:
        job.stem = "Geheimbericht Q3"
        target = job.dir / "src" / "Geheimbericht Q3.docx"
        job.private += [target.as_uri(), str(target), target.name, "Geheimbericht Q3.pdf", job.stem]
        with caplog.at_level(logging.WARNING, logger="heypdf.docs"):
            worker._log_engine_failure(job, "soffice", 1, f"Error: {target} / {target.as_uri()} failed".encode())
        assert "Geheimbericht" not in caplog.text and "engine_failed" in caplog.text
    finally:
        job.cleanup()


# --------------------------------------------------------------------------------- complex-script OCR
def make_script_scan(font: str, lines: list[str], dpi: int = 300) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("L", (int(8.27 * dpi), int(11.69 * dpi)), 255)
    draw = ImageDraw.Draw(img)
    fnt = ImageFont.truetype(font, int(dpi * 0.25))  # raqm shapes conjuncts and pre-base vowel signs
    for i, line in enumerate(lines):
        draw.text((int(dpi * 0.8), int(dpi * 1.2) + i * int(dpi * 0.5)), line, fill=0, font=fnt)
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=dpi)
    return buf.getvalue()


@pytest.mark.parametrize("lang,font,lines,words", [
    ("hin", "NotoSansDevanagari-Regular.ttf", ["भारत एक विशाल देश है।", "वार्षिक प्रतिवेदन २०२६", "कृपया ध्यान दें"],
     ["विशाल", "वार्षिक", "प्रतिवेदन", "कृपया", "ध्यान"]),
    ("ben", "NotoSansBengali-Regular.ttf", ["বার্ষিক প্রতিবেদন", "ভারত একটি বিশাল দেশ"],
     ["বার্ষিক", "প্রতিবেদন", "বিশাল"]),
    ("tam", "NotoSansTamil-Regular.ttf", ["ஆண்டு அறிக்கை", "இந்தியா ஒரு பெரிய நாடு"], ["அறிக்கை", "பெரிய"]),
])
def test_ocr_text_layer_is_searchable_in_indic_scripts(client, lang, font, lines, words):
    """The default renderer's font had no ToUnicode entries for conjuncts / reordered vowel signs: 'वि\\x08शाल'."""
    r = post(client, "ocr", make_script_scan(NOTO + font, lines), options={"languages": [lang]})
    assert r.status_code == 200, r.text
    text = pdf_text(r.content)
    assert [w for w in words if w not in text] == [], text
    assert "(cid:" not in text and not [c for c in text if ord(c) < 0x20 and c not in "\n\r\t\f"], repr(text)


# ------------------------------------------------------------------------------- page rotation
def _page(lines: list[str], size: int, x: int, step: int, dpi: int):
    from PIL import Image, ImageDraw, ImageFont

    s = dpi / 200
    img = Image.new("L", (int(8.27 * dpi), int(11.69 * dpi)), 255)
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((x * s, (200 + i * step) * s), line, fill=0, font=ImageFont.truetype(FONT, int(size * s)))
    return img


INVOICE = ["INVOICE #2026-0042", "Acme Supplies Pvt Ltd", "Date: 12 Oct 2026", "Item: Paper A4 x 10  Rs 2,400",
           "Total due: Rs 2,832", "Thank you for your business"]
RECEIPT = ["CAFE 42", "1 Masala chai   40.00", "TOTAL          100.00"]


def test_ocr_turns_short_upside_down_pages_and_flags_the_ones_it_cannot(client):
    """A 6-line upside-down invoice (OSD confidence ~5) stayed upside down under OCRmyPDF's default threshold (14)
    and got a gibberish text layer, silently. A near-empty page still can't be judged: it gets a warning."""
    dpi = 300
    pages = [_page(INVOICE, 36, 150, 70, dpi).rotate(180), _page(RECEIPT, 30, 500, 50, dpi).rotate(180),
             _page(RECEIPT, 30, 500, 50, dpi)]
    buf = io.BytesIO()
    pages[0].save(buf, "PDF", resolution=dpi, save_all=True, append_images=pages[1:])
    r = post(client, "ocr", buf.getvalue(), options={"languages": ["eng"]})
    assert r.status_code == 200, r.text
    text = pdf_text(r.content)
    assert "INVOICE" in text and "Thank you for your business" in text, text
    meta = info(r)
    assert meta["unturned_pages"] == [2]  # the upright receipt (page 3) is not flagged
    assert any(w.startswith("Page 2 may be upside down") for w in meta["warnings"]), meta["warnings"]


def test_unturned_pages_parser():
    err = ("Starting processing with 3 workers concurrently\n"
           "    2 page is facing ⇩, confidence 0.77 - confidence too low to rotate\n"
           "    1 page is facing ⇩, confidence 5.24 - will rotate ↻\n"
           "   12 with existing rotation ⇨, page is facing ⇦, confidence 1.10 - confidence too low to rotate\n"
           "    3 page is facing ⇧, confidence 0.40 - no change\n").encode()
    assert worker.unturned_pages(err) == [2, 12]


# --------------------------------------------------------------------------- truncated PDFs
@pytest.mark.parametrize("keep", [0.7, 0.9])
def test_repaired_truncated_pdf_reads_back_clean(client, keep):
    """qpdf's trailer rebuilt from a truncated file had no /Size; the 'repaired' file was still damaged."""
    data = make_text_pdf(6)
    r = post(client, "repair", data[: int(len(data) * keep)])
    assert r.status_code == 200, r.text
    with pikepdf.open(io.BytesIO(r.content)) as pdf:
        for _ in pdf.objects:
            pass
        assert pdf.get_warnings() == [] and "/Size" in pdf.trailer
        assert len(pdf.pages) == info(r)["pages_out"] >= 1
    assert info(r)["engine"] == "qpdf" and info(r)["warnings"] == []
