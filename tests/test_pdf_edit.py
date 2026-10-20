"""heypdf Edit tools: page numbers / watermark / crop / edit respect rotation + CropBox; forms fill & flatten."""
from __future__ import annotations

import pymupdf
import pytest
from pydantic import ValidationError

from app.pdf import inspect
from app.pdf import options as O
from app.pdf.edit import format_number
from app.pdf.errors import PdfError
from tests.fixtures.pdf.factory import pdfs, run_tool  # noqa: F401  (session fixture)


def _words(path, page: int) -> list[dict]:
    return inspect.words(str(path), "x.pdf", page)["words"]


def _find(words: list[dict], text: str) -> dict:
    hits = [w for w in words if w["t"] == text]
    assert hits, f"{text!r} not on the page: {[w['t'] for w in words][:30]}"
    return hits[0]


# ------------------------------------------------------------------ page numbers
def test_format_number_tokens():
    assert format_number("Page {n} of {total}", 3, 9) == "Page 3 of 9"
    assert format_number("{roman}-{ROMAN}", 14, 20) == "xiv-XIV"
    with pytest.raises(ValidationError):
        O.PageNumbersOptions(format="{n} {evil}")


def test_page_numbers_land_bottom_right_and_upright_on_every_rotation(pdfs, tmp_path):
    """The fixture has /Rotate 0/90/180/270 with an asymmetric CropBox (and an offset MediaBox)."""
    ctx = run_tool("pdf.page_numbers", tmp_path, [pdfs.geometry],
                   {"format": "Page {n} of {total}", "position": "bottom-right", "size": 14})
    out = ctx.outputs[0][0]
    for n in range(1, 5):
        words = _words(out, n)
        page_w, of_w = _find(words, "Page"), _find(words, "of")
        last = [w for w in words if w["t"] == "4"][-1]
        assert _find(words, str(n))
        # bottom-right of the *visible* page
        assert page_w["x"] > 0.5 and page_w["y"] > 0.85, (n, page_w)
        # upright: a horizontal run that reads left to right on the visible page
        assert page_w["w"] > page_w["h"] * 1.2, (n, page_w)
        assert page_w["x"] < of_w["x"] < last["x"], (n, page_w, of_w, last)
        assert abs(page_w["y"] - last["y"]) < 0.01


def test_page_numbers_facing_pages_and_start(pdfs, tmp_path):
    ctx = run_tool("pdf.page_numbers", tmp_path, [pdfs.report],
                   {"position": "bottom-right", "facing": True, "start": 5, "pages": "2-5"})
    out = ctx.outputs[0][0]
    w2, w3 = _find(_words(out, 2), "5"), _find(_words(out, 3), "6")
    assert w2["x"] > 0.5 and w3["x"] < 0.5  # mirrored on facing pages
    assert not any(w["t"] == "4" and w["y"] > 0.9 and w["x"] < 0.4 for w in _words(out, 1))


# ------------------------------------------------------------------ watermark
def test_text_watermark_on_every_page(pdfs, tmp_path):
    ctx = run_tool("pdf.watermark", tmp_path, [pdfs.geometry], {"text": "DRAFT", "rotation": 45, "opacity": 0.5})
    doc = pymupdf.open(ctx.outputs[0][0])
    for page in doc:
        assert "DRAFT" in page.get_text()


def test_image_watermark_under_content_and_mosaic(pdfs, tmp_path):
    ctx = run_tool("pdf.watermark", tmp_path, [pdfs.report],
                   {"kind": "image", "image_id": "$img", "layer": "under", "mosaic": True, "pages": "1"},
                   extras={"$img": (pdfs.png_alpha, "image")})
    doc = pymupdf.open(ctx.outputs[0][0])
    assert len(doc[0].get_images()) > len(pymupdf.open(pdfs.report)[0].get_images())
    assert any("below the content" in w for w in ctx.warnings)


def test_watermark_image_must_be_an_image(pdfs, tmp_path):
    from app.core.errors import ToolError

    with pytest.raises(ToolError):
        run_tool("pdf.watermark", tmp_path, [pdfs.report], {"kind": "image", "image_id": "$img"},
                 extras={"$img": (pdfs.report, "pdf")})
    with pytest.raises(ValidationError):
        O.WatermarkOptions(kind="text", text="   ")


# ------------------------------------------------------------------ crop
def test_crop_box_is_in_visible_coordinates(pdfs, tmp_path):
    box = {"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5}
    ctx = run_tool("pdf.crop", tmp_path, [pdfs.geometry], {"box": box})
    before, after = pymupdf.open(pdfs.geometry), pymupdf.open(ctx.outputs[0][0])
    for a, b in zip(before, after, strict=True):
        assert b.rect.width == pytest.approx(a.rect.width / 2, abs=1)
        assert b.rect.height == pytest.approx(a.rect.height / 2, abs=1)
        assert b.rotation == a.rotation


def test_crop_remove_outside_deletes_hidden_text(pdfs, tmp_path):
    ctx = run_tool("pdf.crop", tmp_path, [pdfs.report],
                   {"box": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 0.12}, "pages": "1", "remove_outside": True})
    page = pymupdf.open(ctx.outputs[0][0])[0]
    page.set_cropbox(page.mediabox)
    text = page.get_text()
    assert "Chapter 1" in text and "Highlights" not in text


def test_crop_needs_a_box():
    with pytest.raises(ValidationError):
        O.CropOptions(mode="box")
    with pytest.raises(ValidationError):
        O.CropOptions(box={"x": 0.8, "y": 0, "w": 0.5, "h": 0.5})


# ------------------------------------------------------------------ edit
def test_edit_text_lands_top_left_on_every_rotation(pdfs, tmp_path):
    items = [{"type": "text", "page": p, "x": 0.05, "y": 0.04, "w": 0.6, "h": 0.06, "text": f"Note{p}", "size": 14}
             for p in range(1, 5)]
    ctx = run_tool("pdf.edit", tmp_path, [pdfs.geometry], {"items": items})
    for p in range(1, 5):
        w = _find(_words(ctx.outputs[0][0], p), f"Note{p}")
        assert w["x"] < 0.2 and w["y"] < 0.12 and w["w"] > w["h"], (p, w)


def test_edit_shapes_whiteout_image_and_freehand(pdfs, tmp_path):
    items = [
        {"type": "whiteout", "page": 1, "x": 0.0, "y": 0.0, "w": 1.0, "h": 0.06},
        {"type": "rect", "page": 1, "x": 0.1, "y": 0.5, "w": 0.2, "h": 0.1, "stroke": "#ff0000"},
        {"type": "arrow", "page": 1, "points": [[0.1, 0.7], [0.4, 0.75]]},
        {"type": "freehand", "page": 2, "strokes": [[[0.1, 0.1], [0.2, 0.2], [0.3, 0.1]]]},
        {"type": "image", "page": 2, "x": 0.5, "y": 0.5, "w": 0.3, "h": 0.1, "image_id": "$sig"},
    ]
    ctx = run_tool("pdf.edit", tmp_path, [pdfs.report], {"items": items}, extras={"$sig": (pdfs.png_alpha, "image")})
    doc = pymupdf.open(ctx.outputs[0][0])
    assert len(doc[1].get_images()) == 1
    assert len(doc[0].get_drawings()) > len(pymupdf.open(pdfs.report)[0].get_drawings())


def test_edit_highlight_keeps_the_text_dark_like_the_preview(tmp_path):
    """Highlights were painted over the text with normal blending (washed out at 0.45, invisible
    at 1.0); the editor previews them with multiply."""
    import numpy as np

    src = tmp_path / "hl.pdf"
    doc = pymupdf.open()
    for rot in (0, 90):
        page = doc.new_page()
        page.insert_text((72, 100), "HIGHLIGHTED WORDS HERE", fontsize=20)
        page.set_rotation(rot)
    doc.save(src)
    for p, rot in ((1, 0), (2, 90)):
        page = pymupdf.open(src)[p - 1]
        hit = page.search_for("HIGHLIGHTED WORDS HERE")[0] * page.rotation_matrix  # visible coordinates
        W, H = page.rect.width, page.rect.height
        box = {"x": hit.x0 / W - 0.01, "y": hit.y0 / H - 0.005, "w": hit.width / W + 0.02, "h": hit.height / H + 0.01}
        for opacity in (0.45, 1.0):
            ctx = run_tool("pdf.edit", tmp_path, [src], {"items": [
                {"type": "highlight", "page": p, **box, "fill": "#ffe066", "opacity": opacity}]})
            out = pymupdf.open(ctx.outputs[0][0])
            pg = out[p - 1]
            assert [a.type[1] for a in pg.annots()] == ["Highlight"], rot
            pix = pg.get_pixmap(dpi=144, alpha=False, clip=hit)
            a = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w, pix.n).astype(int)
            assert a.sum(axis=2).min() < 150, (rot, opacity)        # glyph pixels stay dark
            bg = np.median(a.reshape(-1, 3), axis=0)
            assert bg[2] < 200 and bg[0] > 200, (rot, opacity, bg)  # and the area is yellow
            assert "HIGHLIGHTED WORDS HERE" in pg.get_text()


def test_edit_item_on_missing_page(pdfs, tmp_path):
    with pytest.raises(PdfError, match="page 9"):
        run_tool("pdf.edit", tmp_path, [pdfs.report],
                 {"items": [{"type": "text", "page": 9, "x": 0, "y": 0, "w": 0.1, "h": 0.1, "text": "x"}]})


# ------------------------------------------------------------------ forms
def test_forms_fill_and_flatten(pdfs, tmp_path):
    values = {"full_name": "Jane Doe", "email": "jane@example.com", "subscribe": True, "plan": "pro",
              "country": "Nepal"}
    ctx = run_tool("pdf.forms", tmp_path, [pdfs.acroform], {"values": values})
    doc = pymupdf.open(ctx.outputs[0][0])
    got = {w.field_name: w.field_value for w in doc[0].widgets()}
    assert got["full_name"] == "Jane Doe" and got["country"] == "Nepal"
    assert got["subscribe"] not in (False, "Off", None)
    assert ctx.data["filled"] >= 5
    flat = run_tool("pdf.forms", tmp_path, [pdfs.acroform], {"values": values, "flatten": True})
    doc = pymupdf.open(flat.outputs[0][0])
    assert not list(doc[0].widgets())
    assert "Jane Doe" in doc[0].get_text() and "Nepal" in doc[0].get_text()


def test_forms_flatten_non_latin_text(pdfs, tmp_path):
    ctx = run_tool("pdf.forms", tmp_path, [pdfs.acroform], {"values": {"full_name": "नमस्ते Jane"}, "flatten": True})
    text = pymupdf.open(ctx.outputs[0][0])[0].get_text()
    assert text.count("Jane") == 1 and "नम" in text  # drawn once, with a font that has the glyphs


def test_forms_reject_unknown_choice(pdfs, tmp_path):
    with pytest.raises(PdfError, match="not one of the choices"):
        run_tool("pdf.forms", tmp_path, [pdfs.acroform], {"values": {"country": "Atlantis"}})


def test_forms_detect_fields_on_flat_form(pdfs, tmp_path):
    ctx = run_tool("pdf.forms", tmp_path, [pdfs.flat_form], {"detect": True})
    doc = pymupdf.open(ctx.outputs[0][0])
    kinds = sorted(w.field_type_string for w in doc[0].widgets())
    assert kinds.count("Text") >= 5 and kinds.count("CheckBox") == 2
    names = {w.field_name for w in doc[0].widgets()}
    assert {"name", "phone"} <= names


def test_forms_create_fields_and_nothing_to_do(pdfs, tmp_path):
    create = [{"type": "text", "page": 1, "name": "extra", "x": 0.1, "y": 0.8, "w": 0.4, "h": 0.03},
              {"type": "combo", "page": 1, "name": "size", "x": 0.6, "y": 0.8, "w": 0.2, "h": 0.03,
               "options": ["S", "M", "L"]}]
    ctx = run_tool("pdf.forms", tmp_path, [pdfs.flat_form], {"create": create, "values": {"size": "M"}})
    got = {w.field_name: w.field_value for w in pymupdf.open(ctx.outputs[0][0])[0].widgets()}
    assert got["size"] == "M" and "extra" in got
    with pytest.raises(PdfError, match="Nothing to do"):
        run_tool("pdf.forms", tmp_path, [pdfs.flat_form], {})
    with pytest.raises(ValidationError):
        O.NewField(type="combo", page=1, name="x", x=0, y=0, w=0.1, h=0.1)


def test_forms_created_on_rotated_pages_flatten_upright_and_whole(tmp_path):
    """Fields heypdf creates on a /Rotate 90/180/270 page carry /MK /R, so the value is drawn
    upright and whole (it used to come out sideways and clipped, e.g. 'VALU')."""
    src = tmp_path / "rot_form.pdf"
    doc = pymupdf.open()
    for rot in (0, 90, 180, 270):
        page = doc.new_page()
        page.insert_text((72, 72), f"Form page rotated {rot}", fontsize=12)
        page.set_rotation(rot)
    doc.save(src)
    value = "VALUE-ON-ROTATED-PAGE"
    create = [{"type": "text", "page": p, "name": f"f{p}", "x": 0.1, "y": 0.3, "w": 0.6, "h": 0.04}
              for p in range(1, 5)]
    ctx = run_tool("pdf.forms", tmp_path, [src],
                   {"create": create, "values": {f"f{p}": value for p in range(1, 5)}, "flatten": True})
    assert not any("sideways" in w for w in ctx.warnings)
    out = pymupdf.open(ctx.outputs[0][0])
    for page in out:
        assert page.get_text().count(value) == 1, (page.rotation, page.get_text())
        m = page.rotation_matrix
        for b in page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                if value in "".join(s["text"] for s in ln["spans"]):
                    dx, dy = ln["dir"]
                    vis = (dx * m.a + dy * m.c, dx * m.b + dy * m.d)  # the direction as the reader sees it
                    assert vis[0] > 0.99 and abs(vis[1]) < 0.01, (page.rotation, ln["dir"])


def test_complex_script_text_added_by_heypdf_is_searchable(pdfs, tmp_path):
    """Devanagari/Bengali/Tamil/Arabic that heypdf adds renders shaped, and its text layer now
    extracts as the original string (it came out as e.g. 'पृȊ 1' / 'नमĚते'), on rotated pages too."""
    ctx = run_tool("pdf.page_numbers", tmp_path, [pdfs.geometry], {"format": "पृष्ठ {n} / {total}", "size": 14})
    out = pymupdf.open(ctx.outputs[0][0])
    for n, page in enumerate(out, start=1):
        assert f"पृष्ठ {n} / 4" in page.get_text(), (n, page.get_text())
        assert len(page.search_for("पृष्ठ")) == 1, n
    texts = ["नमस्ते दुनिया", "রিপোর্ট ২০২৬", "ஆண்டு அறிக்கை", "مرحبا بالعالم", "Plain English"]
    items = [{"type": "text", "page": 1, "x": 0.1, "y": 0.1 + 0.1 * i, "w": 0.6, "h": 0.06, "text": t, "size": 14}
             for i, t in enumerate(texts)]
    edited = pymupdf.open(run_tool("pdf.edit", tmp_path, [pdfs.report], {"items": items}).outputs[0][0])
    got = edited[0].get_text()
    for t in texts:
        assert t in got, (t, got)
        assert edited[0].search_for(t.split()[0]), t
    wm = run_tool("pdf.watermark", tmp_path, [pdfs.report], {"text": "गोपनीय दस्तावेज़", "rotation": 45, "pages": "1"})
    assert "गोपनीय दस्तावेज़" in pymupdf.open(wm.outputs[0][0])[0].get_text()
    # the visible glyphs are still drawn (shaped): the page is not blank where the note is
    pix = edited[0].get_pixmap(dpi=72, clip=pymupdf.Rect(edited[0].rect.width * 0.1, edited[0].rect.height * 0.1,
                                                        edited[0].rect.width * 0.7, edited[0].rect.height * 0.16))
    assert min(pix.samples) < 100
