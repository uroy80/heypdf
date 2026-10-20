"""heypdf Compare: the text diff finds the changed words (with boxes for the UI); the overlay finds pixels."""
from __future__ import annotations

import pymupdf
import pytest

from app.pdf.errors import PdfError
from tests.fixtures.pdf.factory import pdfs, run_tool  # noqa: F401  (session fixture)


def test_text_compare_finds_changed_words(pdfs, tmp_path):
    ctx = run_tool("pdf.compare", tmp_path, [pdfs.report, pdfs.report_v2], {})
    data = ctx.data
    assert not data["identical"] and data["total"] >= 3
    changed = [c for c in data["changes"] if c["type"] == "changed"]
    texts_b = " ".join(c["b"]["text"] for c in data["changes"] if c["b"])
    texts_a = " ".join(c["a"]["text"] for c in data["changes"] if c["a"])
    assert "CHANGED" in texts_b and "magna" in texts_a
    assert "999" in texts_b and "inserted" in texts_b
    assert data["changed_pages_a"] == [2, 3] and data["changed_pages_b"] == [2, 3]
    assert changed and all(0 <= b["x"] <= 1 and 0 <= b["y"] <= 1 for c in changed for b in c["b"]["boxes"])
    # the side-by-side report: one sheet per page pair, highlights baked in
    report = pymupdf.open(ctx.outputs[0][0])
    assert report.page_count == 6 and report[0].rect.width > 2 * 595
    assert ctx.outputs[0][1] == "report_compare.pdf"


def test_identical_documents(pdfs, tmp_path):
    ctx = run_tool("pdf.compare", tmp_path, [pdfs.report, pdfs.report], {"report": False})
    assert ctx.data["identical"] and ctx.data["total"] == 0 and not ctx.outputs


def test_ignore_case(pdfs, tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    for path, word in ((a, "Hello World"), (b, "hello WORLD")):
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 72), word)
        doc.save(path)
    assert run_tool("pdf.compare", tmp_path, [a, b], {"report": False}).data["total"] == 1
    assert run_tool("pdf.compare", tmp_path, [a, b], {"report": False, "ignore_case": True}).data["identical"]


def test_compare_respects_rotation_for_boxes(pdfs, tmp_path):
    other = tmp_path / "geometry_changed.pdf"
    doc = pymupdf.open(pdfs.geometry)
    page = doc[1]  # /Rotate 90
    page.add_redact_annot(page.search_for("marker")[0])
    page.apply_redactions()
    doc.save(other)
    ctx = run_tool("pdf.compare", tmp_path, [pdfs.geometry, other], {"report": False})
    [change] = ctx.data["changes"]
    box = change["a"]["boxes"][0]
    assert change["a"]["pages"] == [2]
    # "marker" sits near the top-right of the visible (rotated) page 2
    assert box["x"] > 0.5 and box["y"] < 0.5 and box["h"] > box["w"]


def test_overlay_compare_marks_changed_regions(pdfs, tmp_path):
    ctx = run_tool("pdf.compare", tmp_path, [pdfs.report, pdfs.report_v2], {"mode": "overlay"})
    pages = {p["page_a"]: p for p in ctx.data["pages"]}
    assert pages[2]["boxes"] and pages[3]["boxes"] and not pages[1]["boxes"]
    assert ctx.data["changed_pages"] == [2, 3]
    assert pymupdf.open(ctx.outputs[0][0]).page_count == 6
    single = run_tool("pdf.compare", tmp_path, [pdfs.report, pdfs.report_v2],
                      {"mode": "overlay", "page_a": 2, "page_b": 2, "report": False})
    assert len(single.data["pages"]) == 1 and single.data["pages"][0]["boxes"]


def test_compare_needs_two_pdfs_and_existing_pages(pdfs, tmp_path):
    with pytest.raises(PdfError, match="exactly two"):
        run_tool("pdf.compare", tmp_path, [pdfs.report], {})
    with pytest.raises(PdfError, match="does not exist"):
        run_tool("pdf.compare", tmp_path, [pdfs.report, pdfs.report_v2], {"mode": "overlay", "page_a": 40})
