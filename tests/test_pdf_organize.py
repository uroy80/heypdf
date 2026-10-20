"""heypdf Organize tools: merge, split, remove / extract pages, organize, rotate (+ input safety)."""
from __future__ import annotations

import pymupdf
import pytest
from pydantic import ValidationError

from app.core import registry
from app.pdf import options as O
from app.pdf.errors import PdfError
from app.pdf.util import parse_pages, parse_ranges
from tests.fixtures.pdf.factory import pdfs, run_tool  # noqa: F401  (session fixture)


def _open(ctx, i: int = 0) -> pymupdf.Document:
    return pymupdf.open(ctx.outputs[i][0])


# ------------------------------------------------------------------ page ranges
def test_parse_ranges_and_pages():
    assert parse_ranges("1-3, 5, 8-last", 10) == [(1, 3), (5, 5), (8, 10)]
    assert parse_ranges("-2, 9-", 10) == [(1, 2), (9, 10)]
    assert parse_pages("odd", 5) == [1, 3, 5]
    assert parse_pages("even, 1", 5) == [1, 2, 4]
    assert parse_pages("", 3) == [1, 2, 3]
    with pytest.raises(ValueError, match="does not exist"):
        parse_pages("4", 3)
    with pytest.raises(ValueError, match="backwards"):
        parse_ranges("3-1", 5)
    with pytest.raises(ValueError):
        O.SplitOptions(ranges="1-2; drop table")


# ------------------------------------------------------------------ merge
def test_merge_keeps_order_and_bookmarks(pdfs, tmp_path):
    ctx = run_tool("pdf.merge", tmp_path, [pdfs.report, pdfs.geometry], {"bookmarks": "per_file"})
    doc = _open(ctx)
    assert doc.page_count == 6 + 4
    assert "Chapter 1" in doc[0].get_text() and "Rotated page 0" in doc[6].get_text()
    toc = doc.get_toc()
    assert toc[0][:2] == [1, "report"] and any(t[1].startswith("Chapter 6") for t in toc)
    assert ctx.outputs[0][1] == "report_merged.pdf"
    assert ctx.data["pages"] == 10


def test_merge_needs_two_files(pdfs, tmp_path):
    with pytest.raises(PdfError, match="at least two"):
        run_tool("pdf.merge", tmp_path, [pdfs.report])
    assert registry.get("pdf.merge").min_files == 2


def test_merge_encrypted_input_asks_for_password(pdfs, tmp_path):
    with pytest.raises(PdfError) as exc:
        run_tool("pdf.merge", tmp_path, [pdfs.encrypted, pdfs.report])
    assert exc.value.needs_password and exc.value.status == 422
    assert exc.value.extra["needs_password"] is True
    with pytest.raises(PdfError) as exc:
        run_tool("pdf.merge", tmp_path, [pdfs.encrypted, pdfs.report], passwords={"0": "nope"})
    assert exc.value.wrong_password
    ctx = run_tool("pdf.merge", tmp_path, [pdfs.encrypted, pdfs.report], passwords={"0": "userpw"})
    assert _open(ctx).page_count == 12
    assert any("not carried over" in w for w in ctx.warnings)


# ------------------------------------------------------------------ split
@pytest.mark.parametrize(("opts", "expected"), [
    ({"mode": "ranges", "ranges": "1-2, 5"}, [2, 1]),
    ({"mode": "fixed", "every": 4}, [4, 2]),
    ({"mode": "all"}, [1] * 6),
    ({"mode": "ranges", "ranges": "1, 3-4", "merge": True}, [3]),
])
def test_split_modes(pdfs, tmp_path, opts, expected):
    ctx = run_tool("pdf.split", tmp_path, [pdfs.report], opts)
    assert [pymupdf.open(p).page_count for p, _n, _m in ctx.outputs] == expected


def test_split_by_size_respects_budget(pdfs, tmp_path):
    budget = 0.02  # MB: every page alone is about this size
    ctx = run_tool("pdf.split", tmp_path, [pdfs.report], {"mode": "size", "max_mb": budget})
    assert len(ctx.outputs) >= 2
    assert sum(pymupdf.open(p).page_count for p, _n, _m in ctx.outputs) == 6
    names = [n for _p, n, _m in ctx.outputs]
    assert names[0].startswith("report_1")


# ------------------------------------------------------------------ remove / extract
def test_remove_pages(pdfs, tmp_path):
    ctx = run_tool("pdf.remove_pages", tmp_path, [pdfs.report], {"pages": "2-3"})
    doc = _open(ctx)
    assert doc.page_count == 4
    text = "".join(p.get_text() for p in doc)
    assert "Chapter 2" not in text and "Chapter 3" not in text and "Chapter 4" in text


def test_remove_every_page_is_refused(pdfs, tmp_path):
    with pytest.raises(PdfError, match="leave at least one"):
        run_tool("pdf.remove_pages", tmp_path, [pdfs.report], {"pages": "all"})
    with pytest.raises(ValidationError):
        O.RemovePagesOptions(pages="")


def test_extract_pages_separately_and_merged(pdfs, tmp_path):
    ctx = run_tool("pdf.extract_pages", tmp_path, [pdfs.report], {"pages": "1, 3"})
    assert [n for _p, n, _m in ctx.outputs] == ["report_page-1.pdf", "report_page-3.pdf"]
    ctx = run_tool("pdf.extract_pages", tmp_path, [pdfs.report], {"pages": "1, 3", "merge": True})
    assert _open(ctx).page_count == 2


# ------------------------------------------------------------------ organize / rotate
def test_organize_reorders_rotates_inserts_blank_and_mixes_files(pdfs, tmp_path):
    plan = {"pages": [{"file": 1, "page": 2, "rotate": 90}, {"blank": True}, {"file": 0, "page": 6},
                      {"file": 0, "page": 1, "rotate": 180}]}
    ctx = run_tool("pdf.organize", tmp_path, [pdfs.report, pdfs.geometry], plan)
    doc = _open(ctx)
    assert doc.page_count == 4
    assert "Rotated page 90" in doc[0].get_text() and doc[0].rotation == (90 + 90) % 360
    assert doc[1].get_text().strip() == ""
    assert "Chapter 6" in doc[2].get_text() and doc[3].rotation == 180


def test_organize_rejects_missing_pages_and_files(pdfs, tmp_path):
    with pytest.raises(PdfError, match="does not exist"):
        run_tool("pdf.organize", tmp_path, [pdfs.report], {"pages": [{"file": 0, "page": 7}]})
    with pytest.raises(PdfError, match="only 1 were given"):
        run_tool("pdf.organize", tmp_path, [pdfs.report], {"pages": [{"file": 2, "page": 1}]})
    with pytest.raises(PdfError, match="no pages"):
        run_tool("pdf.organize", tmp_path, [pdfs.report], {"pages": [{"blank": True}]})
    with pytest.raises(ValidationError):
        O.OrganizeOptions(pages=[{"file": 0}])


def test_rotate_only_landscape(pdfs, tmp_path):
    ctx = run_tool("pdf.rotate", tmp_path, [pdfs.geometry], {"angle": 90, "only": "landscape"})
    doc = _open(ctx)
    # pages 2 and 4 are displayed landscape (/Rotate 90/270 on a portrait MediaBox)
    assert [p.rotation for p in doc] == [0, 180, 180, 0]
    assert ctx.data["rotated_pages"] == 2


# ------------------------------------------------------------------ abuse
def test_postscript_disguised_as_pdf_is_refused(pdfs, tmp_path):
    with pytest.raises(PdfError) as exc:
        run_tool("pdf.rotate", tmp_path, [pdfs.not_pdf])
    assert exc.value.status == 415


def test_truncated_pdf_is_repaired_or_refused_cleanly(pdfs, tmp_path):
    try:
        ctx = run_tool("pdf.rotate", tmp_path, [pdfs.broken])
    except PdfError as exc:
        assert exc.status in (413, 422) and "damaged" in exc.detail
    else:
        assert ctx.outputs


def test_page_cap_is_enforced(tmp_path, monkeypatch):
    from app.pdf import organize

    doc = pymupdf.open()
    for _ in range(12):
        doc.new_page()
    path = tmp_path / "many.pdf"
    doc.save(path)
    monkeypatch.setattr(organize, "MAX_PAGES", 10)  # engines are called in-process here
    from app.pdf.util import Src

    with pytest.raises(PdfError) as exc:
        organize.rotate([Src("x", "many.pdf", str(path))], O.RotateOptions(), str(tmp_path))
    assert exc.value.status == 413
