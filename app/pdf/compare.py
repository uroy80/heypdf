"""Compare two PDFs: a word-level text diff (primary — it ignores reflow noise) and a visual overlay diff.

Text mode returns every change with its page and boxes (visible-page fractions) for the side-by-side UI,
and optionally a report PDF (old | new on one sheet per page pair, changes highlighted). Overlay mode
renders page pairs, marks the regions whose pixels differ and builds an overlay report (old in red, new in
blue, unchanged in grey).
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path

import numpy as np
import pymupdf

from app.pdf import options as O
from app.pdf.errors import PdfError
from app.pdf.util import Result, Src, open_pdf, out_name, upright

MAX_PAGES = 500
MAX_WORDS = 150_000
MAX_CHANGES = 1000
MAX_SIDE_BOXES = 60
MAX_TOTAL_BOXES = 20_000  # keeps the job's JSON result small
MAX_OVERLAY_PAGES = 100
OVERLAY_DPI = 72
RED, GREEN = (0.93, 0.27, 0.27), (0.18, 0.7, 0.35)


Word = tuple[int, pymupdf.Rect, str, pymupdf.Rect]  # (page index, visible box, text, drawing box)


def _single_thread() -> None:
    """One OpenCV thread in the sandbox: worker threads would each reserve address space (RLIMIT_AS)."""
    import cv2

    cv2.setNumThreads(1)


def _words(doc: pymupdf.Document) -> list[Word]:
    """Words in reading order with their visible-page box (rotation and CropBox applied)."""
    out: list[Word] = []
    for page in doc:
        with upright(page) as g:
            for w in page.get_text("words", sort=True):
                text = w[4].strip()
                if not text:
                    continue
                draw = pymupdf.Rect(w[:4])
                vis = g.rect_from_draw(draw)
                if vis.x1 < 0 or vis.y1 < 0 or vis.x0 > g.width or vis.y0 > g.height:
                    continue  # outside the CropBox: not visible, not compared
                out.append((page.number, vis, text, draw))
                if len(out) > MAX_WORDS:
                    raise PdfError(413, f"The documents have more than {MAX_WORDS} words; compare fewer pages")
    return out


def _frac(doc: pymupdf.Document, pno: int, r: pymupdf.Rect) -> dict:
    W, H = doc[pno].rect.width or 1, doc[pno].rect.height or 1
    x0, y0 = max(0.0, r.x0 / W), max(0.0, r.y0 / H)
    return {"x": round(x0, 5), "y": round(y0, 5),
            "w": round(min(1.0 - x0, r.width / W), 5), "h": round(min(1.0 - y0, r.height / H), 5)}


def _norm(word: str, ignore_case: bool) -> str:
    word = re.sub(r"[‘’]", "'", re.sub(r"[“”]", '"', word))
    return word.lower() if ignore_case else word


def _side(doc: pymupdf.Document, items: list[Word], budget: list[int]) -> dict | None:
    if not items:
        return None
    pages = sorted({w[0] for w in items})
    take = max(0, min(MAX_SIDE_BOXES, budget[0], len(items)))
    budget[0] -= take
    boxes = [{"page": w[0] + 1, **_frac(doc, w[0], w[1])} for w in items[:take]]
    return {"text": " ".join(w[2] for w in items)[:2000], "pages": [p + 1 for p in pages], "boxes": boxes}


def text_diff(a: pymupdf.Document, b: pymupdf.Document, ignore_case: bool) -> tuple[list[dict], list[tuple]]:
    wa, wb = _words(a), _words(b)
    sm = difflib.SequenceMatcher(None, [_norm(w[2], ignore_case) for w in wa],
                                 [_norm(w[2], ignore_case) for w in wb], autojunk=False)
    changes: list[dict] = []
    raw: list[tuple] = []
    budget = [MAX_TOTAL_BOXES]
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        kind = {"replace": "changed", "delete": "removed", "insert": "added"}[tag]
        raw.append((kind, wa[i1:i2], wb[j1:j2]))
        if len(changes) < MAX_CHANGES:
            changes.append({"type": kind, "a": _side(a, wa[i1:i2], budget), "b": _side(b, wb[j1:j2], budget)})
    return changes, raw


def _highlight(doc: pymupdf.Document, items: list[Word], rgb: tuple) -> None:
    by_page: dict[int, list[pymupdf.Rect]] = {}
    for w in items:
        by_page.setdefault(w[0], []).append(w[3])
    for p, rects in by_page.items():
        page = doc[p]
        with upright(page):
            for r in rects:
                annot = page.add_highlight_annot(r)
                annot.set_colors(stroke=rgb)
                annot.set_opacity(0.45)
                annot.update()


def _side_by_side(a: pymupdf.Document, b: pymupdf.Document, pairs: list[tuple[int | None, int | None]],
                  title_a: str, title_b: str) -> pymupdf.Document:
    rep = pymupdf.open()
    gap, head = 24.0, 28.0
    for pa, pb in pairs:
        ra = a[pa].rect if pa is not None else pymupdf.Rect(0, 0, 595, 842)
        rb = b[pb].rect if pb is not None else pymupdf.Rect(0, 0, 595, 842)
        h = max(ra.height, rb.height)
        sheet = rep.new_page(width=ra.width + rb.width + gap, height=h + head)
        sheet.insert_text((6, 18), f"{title_a} — page {pa + 1}" if pa is not None else f"{title_a} — (no page)",
                          fontsize=9, color=(0.6, 0.1, 0.1))
        sheet.insert_text((ra.width + gap + 6, 18),
                          f"{title_b} — page {pb + 1}" if pb is not None else f"{title_b} — (no page)",
                          fontsize=9, color=(0.1, 0.45, 0.2))
        if pa is not None:
            sheet.show_pdf_page(pymupdf.Rect(0, head, ra.width, head + ra.height), a, pa)
        if pb is not None:
            sheet.show_pdf_page(pymupdf.Rect(ra.width + gap, head, ra.width + gap + rb.width, head + rb.height), b, pb)
        sheet.draw_line((ra.width + gap / 2, head), (ra.width + gap / 2, head + h), color=(0.7, 0.7, 0.7), width=0.5)
    return rep


def _gray(page: pymupdf.Page, dpi: int, size: tuple[int, int] | None = None) -> np.ndarray:
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
    arr = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.w)
    if size is not None and (pix.w, pix.h) != size:
        import cv2

        arr = cv2.resize(arr, size, interpolation=cv2.INTER_AREA)
    return arr


def overlay_diff(a: pymupdf.Document, b: pymupdf.Document, pairs: list[tuple[int, int]], sensitivity: int,
                 out_dir: str, report: bool) -> tuple[list[dict], Path | None]:
    import cv2

    _single_thread()
    pages: list[dict] = []
    rep = pymupdf.open() if report else None
    for pa, pb in pairs:
        ga = _gray(a[pa], OVERLAY_DPI)
        gb = _gray(b[pb], OVERLAY_DPI, (ga.shape[1], ga.shape[0]))
        diff = cv2.absdiff(ga, gb)
        _, th = cv2.threshold(diff, sensitivity, 255, cv2.THRESH_BINARY)
        th = cv2.dilate(th, np.ones((5, 5), np.uint8))
        cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        H, W = ga.shape
        boxes = []
        for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:300]:
            x, y, w, h = cv2.boundingRect(c)
            if w * h < 9:
                continue
            boxes.append({"x": round(x / W, 5), "y": round(y / H, 5), "w": round(w / W, 5), "h": round(h / H, 5)})
        pages.append({"page_a": pa + 1, "page_b": pb + 1, "boxes": boxes,
                      "changed_ratio": round(float((th > 0).mean()), 5)})
        if rep is not None:
            # old content in red, new content in blue, shared content dark grey
            rgb = np.dstack([gb, np.minimum(ga, gb), ga])
            over = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            for bx in boxes:
                x, y, w, h = (int(bx["x"] * W), int(bx["y"] * H), int(bx["w"] * W), int(bx["h"] * H))
                cv2.rectangle(over, (x - 2, y - 2), (x + w + 2, y + h + 2), (40, 160, 40), 1)
            ok, png = cv2.imencode(".png", over)
            if ok:
                page = rep.new_page(width=a[pa].rect.width, height=a[pa].rect.height)
                page.insert_image(page.rect, stream=png.tobytes())
    path = None
    if rep is not None and rep.page_count:
        path = Path(out_dir) / "overlay.pdf"
        rep.save(path, garbage=3, deflate=True)
    return pages, path


def compare(srcs: list[Src], opts: O.CompareOptions, out_dir: str) -> Result:
    res = Result()
    pdfs = [s for s in srcs if s.kind == "pdf"]
    if len(pdfs) != 2:
        raise PdfError(422, "Choose exactly two PDFs to compare")
    sa, sb = pdfs
    a = open_pdf(sa, max_pages=MAX_PAGES, result=res)
    b = open_pdf(sb, max_pages=MAX_PAGES, result=res)
    res.data.update(pages_a=a.page_count, pages_b=b.page_count, mode=opts.mode)
    if opts.mode == "text":
        changes, raw = text_diff(a, b, opts.ignore_case)
        if not any(page.get_text().strip() for page in a) and not any(page.get_text().strip() for page in b):
            res.warn("Neither PDF has a text layer (scanned?); use the visual overlay, or run OCR PDF first")
        counts = {"added": 0, "removed": 0, "changed": 0}
        for kind, _wa, _wb in raw:
            counts[kind] += 1
        pages_a = sorted({w[0] + 1 for _k, wa, _wb in raw for w in wa})
        pages_b = sorted({w[0] + 1 for _k, _wa, wb in raw for w in wb})
        res.data.update(changes=changes, counts=counts, total=len(raw), truncated=len(raw) > len(changes),
                        changed_pages_a=pages_a, changed_pages_b=pages_b, identical=not raw)
        if opts.report:
            for kind, wa, wb in raw:
                _highlight(a, wa, RED if kind != "added" else GREEN)
                _highlight(b, wb, GREEN)
            for d in (a, b):
                d.bake(annots=True, widgets=True)  # show_pdf_page shows page content only
            n = max(a.page_count, b.page_count)
            pairs = [(i if i < a.page_count else None, i if i < b.page_count else None) for i in range(n)]
            rep = _side_by_side(a, b, pairs, _short(sa.name), _short(sb.name))
            path = Path(out_dir) / "comparison.pdf"
            rep.save(path, garbage=3, deflate=True)
            res.add(path, out_name(sa.name, "compare", "pdf"))
    else:
        if opts.page_a or opts.page_b:
            pa, pb = (opts.page_a or 1) - 1, (opts.page_b or opts.page_a or 1) - 1
            if pa >= a.page_count or pb >= b.page_count:
                raise PdfError(422, "That page does not exist in one of the documents")
            pairs = [(pa, pb)]
        else:
            n = min(a.page_count, b.page_count)
            if a.page_count != b.page_count:
                res.warn(f"The documents have {a.page_count} and {b.page_count} pages; the first {n} pairs were compared")
            if n > MAX_OVERLAY_PAGES:
                res.warn(f"Only the first {MAX_OVERLAY_PAGES} page pairs were compared visually")
            pairs = [(i, i) for i in range(min(n, MAX_OVERLAY_PAGES))]
        pages, path = overlay_diff(a, b, pairs, opts.sensitivity, out_dir, opts.report)
        res.data.update(pages=pages, changed_pages=[p["page_a"] for p in pages if p["boxes"]],
                        identical=not any(p["boxes"] for p in pages))
        if path is not None:
            res.add(path, out_name(sa.name, "overlay", "pdf"))
    return res


def _short(name: str) -> str:
    return name if len(name) <= 60 else name[:57] + "…"
