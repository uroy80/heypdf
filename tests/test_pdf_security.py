"""heypdf Security tools: protect/unlock round trip, true redaction (verified in the content streams),
visual + PAdES signing (validated with pyHanko, tamper detected), no password cracking."""
from __future__ import annotations

import re

import pikepdf
import pymupdf
import pytest
from pydantic import ValidationError

from app.pdf import options as O
from app.pdf.errors import PdfError
from app.pdf.security import PRESETS, _accept, validate_signatures
from tests.fixtures.pdf.factory import pdfs, run_tool  # noqa: F401  (session fixture)


def _streams(path) -> bytes:
    """Every decoded stream of the file, concatenated (content streams, XObjects, fonts …)."""
    out = []
    with pikepdf.open(path) as pdf:
        for obj in pdf.objects:
            if isinstance(obj, pikepdf.Stream):
                try:
                    out.append(obj.read_bytes())
                except pikepdf.PdfError:
                    continue
    return b"\n".join(out)


def _in_streams(secret: str, streams: bytes) -> bool:
    """Literal or hex-string occurrence of ``secret`` in decoded streams (MuPDF writes hex strings)."""
    raw = secret.encode()
    return raw in streams or raw.hex().encode() in streams.lower()


def _text(path) -> str:
    return "\n".join(p.get_text() for p in pymupdf.open(path))


# ------------------------------------------------------------------ protect / unlock
def test_protect_unlock_round_trip(pdfs, tmp_path):
    ctx = run_tool("pdf.protect", tmp_path, [pdfs.report], {"password": "s3cret!", "allow_print": False})
    protected = ctx.outputs[0][0]
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(protected)
    with pikepdf.open(protected, password="s3cret!") as pdf:
        assert pdf.encryption.R == 6 and pdf.encryption.stream_method.name == "aesv3"
        assert pdf.allow.print_highres is False
        assert len(pdf.pages) == 6
    assert ctx.data["encryption"] == "AES-256" and "print" in ctx.data["restricted"]
    back = run_tool("pdf.unlock", tmp_path, [protected], {"password": "s3cret!"})
    with pikepdf.open(back.outputs[0][0]) as pdf:
        assert not pdf.is_encrypted and len(pdf.pages) == 6
    assert "Chapter 1" in _text(back.outputs[0][0])


def test_unlock_wrong_or_missing_password(pdfs, tmp_path):
    with pytest.raises(PdfError) as exc:
        run_tool("pdf.unlock", tmp_path, [pdfs.encrypted], {"password": "guess"})
    assert exc.value.needs_password and exc.value.wrong_password and exc.value.status == 422
    with pytest.raises(PdfError) as exc:
        run_tool("pdf.unlock", tmp_path, [pdfs.encrypted], {})
    assert exc.value.needs_password and not exc.value.wrong_password


def test_unlock_owner_only_restrictions_without_password(pdfs, tmp_path):
    ctx = run_tool("pdf.unlock", tmp_path, [pdfs.owner_only], {})
    with pikepdf.open(ctx.outputs[0][0]) as pdf:
        assert not pdf.is_encrypted
    assert ctx.data["files"][0]["restrictions"] is True


def test_unlock_job_password_map_is_used(pdfs, tmp_path):
    ctx = run_tool("pdf.unlock", tmp_path, [pdfs.encrypted], {}, passwords={"0": "userpw"})
    with pikepdf.open(ctx.outputs[0][0]) as pdf:
        assert not pdf.is_encrypted


def test_protect_options_validation():
    with pytest.raises(ValidationError):
        O.ProtectOptions()
    with pytest.raises(ValidationError):
        O.ProtectOptions(owner_password="x")  # no open password and nothing restricted
    with pytest.raises(ValidationError):
        O.ProtectOptions(password="same", owner_password="same")
    assert O.ProtectOptions(owner_password="x", allow_copy=False)


# ------------------------------------------------------------------ redact
def test_redaction_removes_text_from_content_streams(pdfs, tmp_path):
    before = _streams(pdfs.report)
    assert _in_streams("Bengaluru", before)  # simple fonts keep the text readable in the stream
    ctx = run_tool("pdf.redact", tmp_path, [pdfs.report],
                   {"terms": ["Bengaluru"], "presets": ["email", "phone", "card"], "label": "REDACTED"})
    out = ctx.outputs[0][0]
    text, streams = _text(out), _streams(out)
    for secret in ("Bengaluru", "jane.doe@example.com", "98450", "4111"):
        assert secret not in text
        assert not _in_streams(secret, streams)
    assert "REDACTED" in text
    assert "quarterly report shows" in text  # the rest survives
    assert ctx.data["verified"] is True and ctx.data["by_kind"]["email"] == 3
    # sanitize: metadata and the mailto-free link survive only where untouched
    meta = pymupdf.open(out).metadata
    assert not meta.get("author") and not meta.get("subject")
    # table numbers are not mistaken for phone numbers
    assert "Region 1" in text and re.search(r"\b30[1-5]\b", text)


def test_redaction_keeps_the_lines_above_and_below_a_match(tmp_path):
    """Tight leading makes line boxes overlap; a match must not take the glyphs above/below it with it."""
    src = tmp_path / "tight.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    lines = ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
             "bbbbbbbbbb SECRETWORD bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
             "cccccccccccccccccccccccccccccccccccccccccccccccccccc"]
    for i, line in enumerate(lines):  # 12 pt text on a 12 pt pitch: line boxes overlap by ~15 %
        page.insert_text((72, 100 + 12 * i), line, fontsize=12, fontname="helv")
    doc.save(src)
    boxes = [ln["bbox"] for b in pymupdf.open(src)[0].get_text("dict")["blocks"] for ln in b["lines"]]
    assert any(b1[3] > b2[1] for b1, b2 in zip(boxes, boxes[1:], strict=False))  # the overlap is real
    ctx = run_tool("pdf.redact", tmp_path, [str(src)], {"terms": ["SECRETWORD"]})
    text = _text(ctx.outputs[0][0])
    assert "SECRETWORD" not in text
    assert lines[0] in text and lines[2] in text  # neighbours intact
    assert "bbbbbbbbbb" in text and "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" in text


def test_redaction_of_an_area_blanks_image_pixels_and_text(pdfs, tmp_path):
    src = pymupdf.open(pdfs.report)
    page = src[0]
    img_box = page.get_image_info()[0]["bbox"]
    W, H = page.rect.width, page.rect.height
    area = {"page": 1, "x": img_box[0] / W, "y": img_box[1] / H, "w": (img_box[2] - img_box[0]) / W / 2,
            "h": (img_box[3] - img_box[1]) / H / 2}
    title = page.search_for("Highlights")[0]
    words_area = {"page": 1, "x": title.x0 / W - 0.005, "y": title.y0 / H - 0.005, "w": title.width / W + 0.01,
                  "h": title.height / H + 0.01}
    ctx = run_tool("pdf.redact", tmp_path, [pdfs.report], {"areas": [area, words_area], "fill": "#ffffff"})
    out = pymupdf.open(ctx.outputs[0][0])
    assert "Highlights" not in out[0].get_text()
    xref = out[0].get_images()[0][0]
    pix = pymupdf.Pixmap(out, xref)
    import numpy as np

    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n)
    tl = a[: pix.h // 2 - 4, : pix.w // 2 - 4]
    br = a[pix.h // 2 + 4:, pix.w // 2 + 4:]
    assert tl.std() < 2.0 and br.std() > 5.0  # the covered quarter is flat, the rest is still a photo


def test_redaction_regex_case_and_whole_words(pdfs, tmp_path):
    ctx = run_tool("pdf.redact", tmp_path, [pdfs.report],
                   {"regex": [r"Region \d"], "terms": ["BOARD"], "case_sensitive": True, "pages": "1"})
    text = pymupdf.open(ctx.outputs[0][0])[0].get_text()
    assert "Region 1" not in text and "board" in text  # case-sensitive term did not match "board"
    assert "Region 1" in pymupdf.open(ctx.outputs[0][0])[2].get_text()  # other pages untouched


def test_redaction_full_pages(pdfs, tmp_path):
    ctx = run_tool("pdf.redact", tmp_path, [pdfs.report], {"full_pages": "2"})
    out = pymupdf.open(ctx.outputs[0][0])
    assert out[1].get_text().strip() == "" and out[1].get_images() == []
    assert "Chapter 3" in out[2].get_text()


def test_redaction_nothing_matched_and_bad_input(pdfs, tmp_path):
    with pytest.raises(PdfError, match="Nothing matched"):
        run_tool("pdf.redact", tmp_path, [pdfs.report], {"terms": ["zebra-crossing"]})
    with pytest.raises(ValidationError):
        O.RedactOptions()
    with pytest.raises(ValidationError):
        O.RedactOptions(regex=["(unclosed"])


def _pixels(path, page_no: int = 0, dpi: int = 150):
    import numpy as np

    pix = pymupdf.open(path)[page_no].get_pixmap(dpi=dpi, alpha=False)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n).astype(int), dpi / 72


def _all_objects_text(path) -> str:
    """Every object's source and every decompressed stream, as text (to look for leftovers anywhere)."""
    doc = pymupdf.open(path)
    parts = []
    for xref in range(1, doc.xref_length()):
        try:
            parts.append(doc.xref_object(xref, compressed=False))
            if doc.xref_is_stream(xref):
                parts.append((doc.xref_stream(xref) or b"").decode("latin-1"))
        except Exception:
            continue
    return "\n".join(parts)


def test_redaction_keeps_backgrounds_table_fills_and_banners(tmp_path):
    """A term box must not delete the page background, a navy header cell (with white text) or
    the shading of the cell it sits in; the old default (line art 'touched') erased all three."""
    src = tmp_path / "designed.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.draw_rect(page.rect, color=None, fill=(245 / 255, 240 / 255, 219 / 255))       # page background
    page.draw_rect(pymupdf.Rect(72, 150, 200, 172), color=None, fill=(0.1, 0.15, 0.4))  # navy header cell
    page.insert_text((80, 166), "Email", fontsize=12, color=(1, 1, 1))
    page.draw_rect(pymupdf.Rect(200, 150, 420, 172), color=None, fill=(0.8, 0.9, 1.0))   # light-blue cell
    page.insert_text((206, 166), "ZX-4471-Q", fontsize=12)
    page.draw_rect(pymupdf.Rect(72, 400, 540, 440), color=None, fill=(0.2, 0.3, 0.6))   # banner
    page.insert_text((80, 425), "Quarterly banner text", fontsize=14, color=(1, 1, 1))
    doc.save(src)
    before, k = _pixels(src)
    ctx = run_tool("pdf.redact", tmp_path, [src], {"terms": ["ZX-4471-Q"]})
    out = ctx.outputs[0][0]
    after, _ = _pixels(out)
    assert ctx.data["verified"] is True and "ZX-4471-Q" not in _text(out)

    def px(x, y):
        return after[int(y * k), int(x * k)], before[int(y * k), int(x * k)]

    for x, y in ((40, 40), (300, 700), (190, 152), (410, 152), (500, 405)):  # background, navy, blue, banner
        a, b = px(x, y)
        assert abs(a - b).max() <= 2, (x, y, a, b)
    # the white header text still shows on its navy fill
    cell = after[int(152 * k):int(171 * k), int(78 * k):int(120 * k)]
    assert (cell.min(axis=2) > 230).sum() > 20 and (cell.max(axis=2) < 120).sum() > 20
    assert "Email" in _text(out) and "Quarterly banner text" in _text(out)
    # the box itself is there (black where the term was)
    box = after[int(158 * k):int(164 * k), int(212 * k):int(250 * k)]
    assert box.max() < 60
    # an area drawn over the term keeps them as well (default: only fully covered line art goes)
    area = {"page": 1, "x": 204 / page.rect.width, "y": 154 / page.rect.height, "w": 70 / page.rect.width,
            "h": 15 / page.rect.height}
    out2 = run_tool("pdf.redact", tmp_path, [src], {"areas": [area]}).outputs[0][0]
    after2, _ = _pixels(out2)
    for x, y in ((40, 40), (190, 152), (410, 152)):
        assert abs(after2[int(y * k), int(x * k)] - before[int(y * k), int(x * k)]).max() <= 2, (x, y)
    assert O.RedactOptions(terms=["x"]).graphics == "covered"


def test_redaction_removes_the_term_from_comments_bookmarks_and_metadata(tmp_path):
    """A sticky note away from the text (contents + author), a bookmark title, a page label and
    the document title must not keep the redacted name; nor may any object of the file."""
    src = tmp_path / "case.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Patient Rahul Verma was seen on Monday.", fontsize=12)
    note = page.add_text_annot((450, 650), "Reviewer: call Rahul Verma back tomorrow")
    note.set_info(title="Dr. Rahul Verma", subject="Follow-up")
    note.update()
    keep = page.add_text_annot((450, 700), "Unrelated reminder")
    keep.update()
    doc.new_page().insert_text((72, 100), "Second page", fontsize=12)
    doc.set_toc([[1, "Rahul Verma - case file", 1], [1, "Appendix", 2]])
    doc.set_metadata({"title": "Case of Rahul Verma", "author": "Clinic"})
    doc.set_page_labels([{"startpage": 0, "prefix": "Rahul Verma-", "style": "D", "firstpagenum": 1}])
    doc.save(src)
    for sanitize in (False, True):
        ctx = run_tool("pdf.redact", tmp_path, [src], {"terms": ["Rahul Verma"], "sanitize": sanitize})
        out = ctx.outputs[0][0]
        assert ctx.data["verified"] is True
        res = pymupdf.open(out)
        assert [a.info["content"] for a in res[0].annots()] == ["Unrelated reminder"]
        assert [t[1] for t in res.get_toc()] == ["[redacted] - case file", "Appendix"]
        assert "Rahul" not in (res.metadata.get("title") or "")
        assert all("Rahul" not in (lb.get("prefix") or "") for lb in res.get_page_labels())
        assert "Rahul" not in _all_objects_text(out)
        assert ctx.data["also_removed"]["annotations"] == 1 and any("comment" in w for w in ctx.warnings)
    # the term only in a comment (no page text): still redacted, not "Nothing matched"
    only = tmp_path / "only_note.pdf"
    doc = pymupdf.open()
    doc.new_page().add_text_annot((100, 100), "mail jane.doe@example.com").update()
    doc.save(only)
    ctx = run_tool("pdf.redact", tmp_path, [only], {"presets": ["email"]})
    assert "jane.doe" not in _all_objects_text(ctx.outputs[0][0]) and ctx.data["also_removed"] == {"annotations": 1}


def test_redaction_of_a_filled_form(tmp_path):
    """Values in filled (not flattened) form fields are redacted — the field goes, the box stays —
    instead of failing with an 'unusual text encoding' error."""
    src = tmp_path / "filled.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), "Applicant form", fontsize=14)
    for i, (name, value) in enumerate((("full_name", "SSN 123-45-6789"), ("notes", "jane.doe@example.com"),
                                       ("city", "Pune"))):
        w = pymupdf.Widget()
        w.rect = pymupdf.Rect(72, 120 + 40 * i, 350, 140 + 40 * i)
        w.field_name, w.field_type, w.field_value = name, pymupdf.PDF_WIDGET_TYPE_TEXT, value
        w.text_fontsize = 11
        page.add_widget(w)
    doc.save(src)
    ctx = run_tool("pdf.redact", tmp_path, [src], {"presets": ["ssn", "email"]})
    out = ctx.outputs[0][0]
    assert ctx.data["verified"] is True and ctx.data["matches"] == 2
    res = pymupdf.open(out)
    assert sorted(w.field_name for w in res[0].widgets()) == ["city"]  # untouched field stays
    everything = _all_objects_text(out)
    assert "123-45-6789" not in everything and "jane.doe" not in everything
    assert "Applicant form" in res[0].get_text()
    # an area drawn over a field removes it too
    area = {"page": 1, "x": 70 / page.rect.width, "y": 198 / page.rect.height, "w": 290 / page.rect.width,
            "h": 24 / page.rect.height}
    ctx = run_tool("pdf.redact", tmp_path, [src], {"areas": [area]})
    assert "Pune" not in _all_objects_text(ctx.outputs[0][0])
    assert ctx.data["verified"] is True


def test_redaction_box_spares_neighbouring_lines_glyphs(tmp_path):
    """With tight leading the visible box covers the matched glyphs only — not the descenders of
    the line above nor the cap tops of the line below."""
    import numpy as np

    src = tmp_path / "tight_box.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    size, lead = 12, 12.5
    lines = ["gyjqp gyjqp gyjqp gyjqp gyjqp gyjqp", "HHH ZX-4471-Q-SECRET HHH", "HHHHHHHHHHHHHHHHHHHHHHHHHH"]
    for i, line in enumerate(lines):
        page.insert_text((72, 100 + lead * i), line, fontsize=size, fontname="helv")
    doc.save(src)
    before, k = _pixels(src, dpi=300)
    ctx = run_tool("pdf.redact", tmp_path, [src], {"terms": ["ZX-4471-Q-SECRET"]})
    out = ctx.outputs[0][0]
    after, _ = _pixels(out, dpi=300)
    term = pymupdf.open(src)[0].search_for("ZX-4471-Q-SECRET")[0]
    xs = slice(int(term.x0 * k), int(term.x1 * k))
    above = slice(int((100 + 0.5) * k), int((100 + 0.22 * size) * k))               # descenders of line 1
    below = slice(int((100 + 2 * lead - 0.71 * size) * k), int((100 + 2 * lead - 0.45 * size) * k))  # cap tops
    assert (before[above, xs] < 128).any() and (before[below, xs] < 128).any()     # there is ink to protect
    assert np.abs(after[above, xs] - before[above, xs]).max() <= 40
    assert np.abs(after[below, xs] - before[below, xs]).max() <= 40
    mid = after[int((100 + lead - 4) * k), xs]
    assert mid.max() < 60  # the box is drawn over the term
    assert "ZX-4471-Q" not in _text(out) and ctx.data["verified"] is True


@pytest.mark.parametrize(("kind", "text", "ok"), [
    ("card", "4111 1111 1111 1111", True), ("card", "4111 1111 1111 1112", False),
    ("phone", "+91 98450 12345", True), ("phone", "107 251 301", False), ("phone", "2026-10-18", False),
    ("iban", "GB82 WEST 1234 5698 7654 32", True), ("iban", "GB82 WEST 1234 5698 7654 33", False),
])
def test_preset_validators(kind, text, ok):
    assert bool(re.search(PRESETS[kind], text)) and _accept(kind, text) is ok


def test_catastrophic_regex_is_stopped_by_the_sandbox(tmp_path):
    import time

    from app.pdf import isolate, security
    from app.pdf.util import Src

    doc = pymupdf.open()
    doc.new_page().insert_text((50, 50), "a" * 60 + "!", fontsize=6)
    path = tmp_path / "re.pdf"
    doc.save(path)
    opts = O.RedactOptions(regex=[r"(a+)+$"])
    started = time.monotonic()
    with pytest.raises(Exception) as exc:  # noqa: PT011 — SandboxTimeout (platform) or PdfError (fallback)
        isolate.run(security.redact, [Src("x", "re.pdf", str(path))], opts, str(tmp_path), timeout_s=3, mem_mb=1024)
    assert getattr(exc.value, "status", None) in (413, 422, 504)
    assert time.monotonic() - started < 15


# ------------------------------------------------------------------ sign
def test_visual_signature_typed_with_date_text(pdfs, tmp_path):
    ctx = run_tool("pdf.sign", tmp_path, [pdfs.report], {
        "style": "type", "text": "Jane Doe", "placements": [{"x": 0.6, "y": 0.85, "w": 0.3, "h": 0.08, "pages": "last"}],
        "texts": [{"text": "Signed {date}", "x": 0.6, "y": 0.94, "w": 0.3, "h": 0.03, "pages": "last"}]})
    out = pymupdf.open(ctx.outputs[0][0])
    assert "Jane Doe" in out[-1].get_text() and "Signed" in out[-1].get_text()
    assert "Jane Doe" not in out[0].get_text().split("Contact")[0]
    assert ctx.data["placed"][0]["page"] == 6


def test_drawn_signature_on_rotated_pages(pdfs, tmp_path):
    import base64

    data = "data:image/png;base64," + base64.b64encode(pdfs.png_alpha.read_bytes()).decode()
    ctx = run_tool("pdf.sign", tmp_path, [pdfs.geometry], {
        "style": "draw", "image_data": data, "placements": [{"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.1, "pages": "all"}]})
    out = pymupdf.open(ctx.outputs[0][0])
    for page in out:
        info = page.get_image_info()
        assert info, page.number
        # visible position: the image's box, mapped through the page rotation, is near the top-left
        bbox = pymupdf.Rect(info[0]["bbox"]) * page.rotation_matrix
        assert bbox.x0 < page.rect.width * 0.5 and bbox.y0 < page.rect.height * 0.3, (page.number, bbox)


def test_pades_signature_validates_and_tamper_is_detected(pdfs, tmp_path):
    ctx = run_tool("pdf.sign", tmp_path, [pdfs.report, (pdfs.p12, "cert")], {
        "style": "type", "text": "Jane Doe", "placements": [{"x": 0.6, "y": 0.85, "w": 0.3, "h": 0.08, "pages": "1"}],
        "digital": True, "cert_password": "p12pass", "reason": "Approved", "location": "Bengaluru"})
    signed = ctx.outputs[0][0]
    assert ctx.data["digital"]["level"] == "PAdES B-B" and "Jane Doe" in ctx.data["digital"]["signer"]
    [status] = validate_signatures(str(signed), str(pdfs.ca_pem))
    assert status["intact"] and status["valid"] and status["trusted"]
    [untrusted] = validate_signatures(str(signed))
    assert untrusted["intact"] and not untrusted["trusted"]
    with open(signed, "rb") as fh:
        from pyhanko.pdf_utils.reader import PdfFileReader

        sig = PdfFileReader(fh).embedded_signatures[0]
        assert str(sig.sig_object["/SubFilter"]) == "/ETSI.CAdES.detached"
    data = bytearray(signed.read_bytes())
    i = data.index(b"Chapter") if b"Chapter" in data else 1500
    data[i] ^= 0x01
    tampered = tmp_path / "tampered.pdf"
    tampered.write_bytes(bytes(data))
    [bad] = validate_signatures(str(tampered), str(pdfs.ca_pem))
    assert not bad["intact"] or not bad["valid"]


def test_second_signature_keeps_the_first_valid(pdfs, tmp_path):
    opts = {"style": "none", "digital": True, "cert_password": "p12pass"}
    first = run_tool("pdf.sign", tmp_path, [pdfs.report, (pdfs.p12, "cert")], opts).outputs[0][0]
    second = run_tool("pdf.sign", tmp_path, [first, (pdfs.p12, "cert")], {
        **opts, "style": "type", "text": "Counter", "placements": [{"x": 0.1, "y": 0.9, "w": 0.2, "h": 0.05, "pages": "1"}]})
    statuses = validate_signatures(str(second.outputs[0][0]), str(pdfs.ca_pem))
    assert len(statuses) == 2 and all(s["intact"] and s["valid"] for s in statuses)


def test_sign_certificate_errors(pdfs, tmp_path):
    base = {"style": "none", "digital": True}
    with pytest.raises(PdfError, match="password is wrong"):
        run_tool("pdf.sign", tmp_path, [pdfs.report, (pdfs.p12, "cert")], {**base, "cert_password": "nope"})
    with pytest.raises(PdfError, match="expired"):
        run_tool("pdf.sign", tmp_path, [pdfs.report, (pdfs.p12_expired, "cert")], {**base, "cert_password": "p12pass"})
    with pytest.raises(PdfError, match="certificate file"):
        run_tool("pdf.sign", tmp_path, [pdfs.report], {**base, "cert_password": "p12pass"})
    junk = tmp_path / "junk.p12"
    junk.write_bytes(b"\x30" + b"\x00" * 64)
    with pytest.raises(PdfError):
        run_tool("pdf.sign", tmp_path, [pdfs.report, (junk, "cert")], {**base, "cert_password": "x"})


def test_sign_options_validation():
    with pytest.raises(ValidationError):
        O.SignOptions(style="type", text="", placements=[{"x": 0, "y": 0, "w": 0.1, "h": 0.1}])
    with pytest.raises(ValidationError):
        O.SignOptions(style="type", text="Jane")  # not placed anywhere
    with pytest.raises(ValidationError):
        O.SignOptions(style="none")


def test_sign_date_is_the_signers_local_date():
    """{date} used the server's UTC date; at 01:00 IST (19:30 UTC the day before) the stamp said
    yesterday while the editor previewed today."""
    import datetime as dt

    from app.pdf.security import _today

    late_utc = dt.datetime(2026, 10, 18, 20, 0, tzinfo=dt.UTC)          # 01:30 on the 19th in India
    assert _today("d-m-Y", 330, now=late_utc) == "19-10-2026"
    assert _today("d-m-Y", 0, now=late_utc) == "18-10-2026"
    assert _today("Y-m-d", -300, now=dt.datetime(2026, 10, 19, 3, 0, tzinfo=dt.UTC)) == "2026-10-18"  # New York
    assert O.SignOptions(style="none", digital=True).utc_offset_min == 0
    with pytest.raises(ValidationError):
        O.SignOptions(style="none", digital=True, utc_offset_min=10_000)


def test_sign_stamps_the_offset_date(pdfs, tmp_path):
    import datetime as dt

    offset = 14 * 60  # UTC+14 (Kiribati): always "tomorrow" or "today" relative to UTC, never yesterday
    expect = (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=offset)).strftime("%Y-%m-%d")
    ctx = run_tool("pdf.sign", tmp_path, [pdfs.report], {
        "style": "none", "date_format": "Y-m-d", "utc_offset_min": offset,
        "texts": [{"text": "On {date}", "x": 0.1, "y": 0.9, "w": 0.4, "h": 0.04, "pages": "1"}]})
    assert f"On {expect}" in pymupdf.open(ctx.outputs[0][0])[0].get_text()
