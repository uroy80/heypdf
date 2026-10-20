"""Read-only views of a PDF for the heypdf UI: document info, page renders and word boxes.

All three run inside the sandbox child (``app.pdf.isolate.run``); results are small JSON-able values or
PNG bytes. Boxes are visible-page fractions (rotation and CropBox applied, origin top-left), the same
convention the tool options use, so the UI can overlay them on a rendered page as they are.
"""
from __future__ import annotations

import pymupdf

from app.pdf.errors import PdfError
from app.pdf.forms import list_fields
from app.pdf.util import Src, check_magic, has_signatures, upright

MAX_OUTLINE = 500
MAX_FIELDS = 500
MAX_WORDS = 20_000
MAX_RENDER_SIDE = 2400
MAX_RENDER_PIXELS = 12_000_000
TEXT_PROBE_PAGES = 20


def _open(path: str, name: str, password: str | None = None) -> pymupdf.Document:
    check_magic(path, name)
    try:
        doc = pymupdf.open(path, filetype="pdf")
    except Exception:
        raise PdfError(422, f"“{name}” is damaged and could not be opened") from None
    if doc.needs_pass and not (password and doc.authenticate(password)):
        raise PdfError(422, f"“{name}” is password-protected; unlock it first", needs_password=True,
                       wrong_password=bool(password))
    return doc


def info(path: str, name: str = "document.pdf", password: str | None = None) -> dict:
    """Pages, visible page sizes (pt), rotations, encryption, text layer, form fields and outline."""
    check_magic(path, name)
    try:
        doc = pymupdf.open(path, filetype="pdf")
    except Exception:
        raise PdfError(422, f"“{name}” is damaged and could not be opened") from None
    if doc.needs_pass and not (password and doc.authenticate(password)):
        return {"pages": None, "encrypted": True, "needs_password": True, "page_sizes": [], "rotations": []}
    sizes, rotations = [], []
    for page in doc:
        r = page.rect
        sizes.append([round(r.width, 2), round(r.height, 2)])
        rotations.append(page.rotation)
    has_text = any(doc[i].get_text("text").strip() for i in range(min(doc.page_count, TEXT_PROBE_PAGES)))
    try:
        toc = [{"level": lvl, "title": str(t)[:300], "page": p} for lvl, t, p in doc.get_toc(simple=True)[:MAX_OUTLINE]]
    except Exception:
        toc = []
    try:
        fields = list_fields(doc, limit=MAX_FIELDS)
    except Exception:
        fields = []
    meta = doc.metadata or {}
    return {
        "pages": doc.page_count, "page_sizes": sizes, "rotations": rotations,
        "encrypted": bool(doc.is_encrypted), "needs_password": False,
        "restricted": bool(doc.is_encrypted) and not bool(password),
        "has_text": has_text, "fields": fields, "has_form": bool(fields) or bool(doc.is_form_pdf),
        "outline": toc, "signed": has_signatures(doc), "repaired": bool(doc.is_repaired),
        "title": (meta.get("title") or "")[:300], "pdf_version": (meta.get("format") or "")[:20],
    }


def render_page(path: str, name: str, page_no: int, max_side: int, password: str | None = None) -> bytes:
    """PNG of the visible page (1-based ``page_no``) whose longer side is ``max_side`` px."""
    doc = _open(path, name, password)
    if not 1 <= page_no <= doc.page_count:
        raise PdfError(404, f"Page {page_no} does not exist (the document has {doc.page_count} pages)")
    page = doc[page_no - 1]
    w, h = page.rect.width, page.rect.height
    if w <= 0 or h <= 0:
        raise PdfError(422, f"Page {page_no} has no area")
    side = max(16, min(int(max_side), MAX_RENDER_SIDE))
    zoom = side / max(w, h)
    if w * zoom * h * zoom > MAX_RENDER_PIXELS:
        zoom = (MAX_RENDER_PIXELS / (w * h)) ** 0.5
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False, annots=True)
    return pix.tobytes("png")


def words(path: str, name: str, page_no: int, password: str | None = None) -> dict:
    """Word boxes of one page: ``{"words": [{"t", "x", "y", "w", "h", "b", "l"}], "width", "height"}``."""
    doc = _open(path, name, password)
    if not 1 <= page_no <= doc.page_count:
        raise PdfError(404, f"Page {page_no} does not exist (the document has {doc.page_count} pages)")
    page = doc[page_no - 1]
    out = []
    with upright(page) as g:
        for x0, y0, x1, y1, text, block, line, _wno in page.get_text("words", sort=True):
            box = g.draw_to_frac((x0, y0, x1, y1))
            cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
            if not (0 <= cx <= 1 and 0 <= cy <= 1):
                continue  # outside the CropBox
            out.append({"t": text, **box, "b": block, "l": line})
            if len(out) >= MAX_WORDS:
                break
        width, height = g.width, g.height
    return {"page": page_no, "width": round(width, 2), "height": round(height, 2), "words": out,
            "truncated": len(out) >= MAX_WORDS}


def search_file(path: str, name: str, opts, password: str | None = None) -> dict:
    from app.pdf.security import search

    return search([Src("route", name, path, "pdf", password)], opts)
