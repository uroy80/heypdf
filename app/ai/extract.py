"""Text extraction with PyMuPDF: plain page text (summaries) and positioned segments (layout-keeping translation).

``extract_pages`` and ``extract_segments`` are the entry points the tools run inside
``app.core.sandbox.run_isolated`` (untrusted PDFs never touch MuPDF in the server process). They are top-level
functions with picklable arguments and results, and they report problems as ``{"ok": False, "kind", "message"}``
instead of raising, so the result crosses the process boundary whatever the sandbox does with exceptions.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

_FLAGS = (pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES) | pymupdf.TEXT_MEDIABOX_CLIP
MAX_PAGE_CHARS = 200_000          # a page with more text than this is malformed (or an attack); it is cut


class NeedsOCR(Exception):
    """The document (or too much of it) has no text layer."""


@dataclass
class PageText:
    number: int        # 1-based
    text: str
    scanned: bool      # no visible text but images cover the page


def _fail(kind: str, message: str) -> dict:
    return {"ok": False, "kind": kind, "message": message}


def open_pdf(path: str, password: str | None) -> pymupdf.Document | dict:
    """Open an untrusted PDF (magic-checked); returns the document or a failure dict."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(1024)
    except OSError:
        return _fail("broken", "The file could not be read")
    if b"%PDF-" not in head:
        return _fail("broken", "This file is not a PDF")
    try:
        doc = pymupdf.open(path, filetype="pdf")
    except Exception:  # noqa: BLE001 - MuPDF raises several types for damaged files
        return _fail("broken", "This PDF is damaged and cannot be read (try Repair PDF first)")
    if doc.needs_pass and not (password and doc.authenticate(password)):
        doc.close()
        return _fail("needs_password", "This PDF is password-protected; enter its password to continue"
                     if not password else "The password for this PDF is not correct")
    return doc


def _is_scanned(page: pymupdf.Page, text: str) -> bool:
    if text.strip():
        return False
    area = abs(page.rect)
    covered = sum(abs(pymupdf.Rect(i["bbox"]) & page.rect) for i in page.get_image_info())
    return area > 0 and covered / area > 0.5


def page_texts(doc: pymupdf.Document, max_pages: int) -> list[PageText]:
    if doc.page_count > max_pages:
        raise ValueError(f"The AI tools take up to {max_pages} pages; this PDF has {doc.page_count}")
    out = []
    for page in doc:
        # Content-stream order (sort=False): 12x faster than sort=True and it does not interleave the columns of a
        # two-column page the way a (y, x) sort does (verified in the research spike).
        text = page.get_text("text")[:MAX_PAGE_CHARS]
        out.append(PageText(page.number + 1, text.strip(), _is_scanned(page, text)))
    return out


def extract_pages(path: str, password: str | None, max_pages: int) -> dict:
    """Sandbox entry point: page texts of one PDF."""
    doc = open_pdf(path, password)
    if isinstance(doc, dict):
        return doc
    try:
        if doc.page_count == 0:
            return _fail("broken", "This PDF has no pages")
        if doc.page_count > max_pages:
            return _fail("too_many_pages", f"The AI tools take up to {max_pages} pages; this PDF has "
                                           f"{doc.page_count}")
        title = (doc.metadata or {}).get("title") or None
        return {"ok": True, "pages": page_texts(doc, max_pages), "page_count": doc.page_count, "title": title}
    except Exception:  # noqa: BLE001
        return _fail("broken", "This PDF is damaged and its text cannot be read (try Repair PDF first)")
    finally:
        doc.close()


def document_xml(pages: list[PageText], title: str | None = None, index: int | None = None) -> str:
    """Wrap pages in light XML so the model can cite page numbers and cannot confuse the document with
    instructions (the text is escaped, so a document cannot close the wrapper)."""
    attrs = (f' index="{index}"' if index is not None else "") + (f' title="{_attr(title)}"' if title else "")
    head = f'<document{attrs} pages="{len(pages)}">'
    body = "\n".join(f'<page n="{p.number}">\n{_esc(p.text)}\n</page>' for p in pages if p.text)
    return f"{head}\n{body}\n</document>"


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attr(s: str | None) -> str:
    return _esc(s or "").replace('"', "&quot;")


# ---- translation segments ---------------------------------------------------------------------------------------
@dataclass
class Segment:
    id: str
    page: int                 # 0-based
    bbox: tuple[float, float, float, float]
    text: str
    size: float
    color: str                # "#rrggbb"
    bold: bool
    italic: bool
    family: str               # "serif" | "sans-serif" | "monospace"
    align: str                # "left" | "center" | "right" | "justify"
    invisible: bool = False   # OCR text layer (alpha 0) over a scanned image
    role: str = "body"        # "body" | "header" | "footer"
    translate: bool = True
    lines: int = 1
    key: str = field(default="", repr=False)   # dedupe key (identical source text is translated once)


_NO_LETTERS = re.compile(r"^[\W\d_]+$", re.UNICODE)
_URLISH = re.compile(r"^(https?://|www\.)\S+$|^\S+@\S+\.\w+$")


def _hex(color: int) -> str:
    return f"#{color & 0xFFFFFF:06x}"


def _family(font: str, flags: int) -> str:
    f = font.lower()
    if flags & 8 or any(k in f for k in ("mono", "courier", "consol")):
        return "monospace"
    if flags & 4 or any(k in f for k in ("times", "serif", "roman", "georgia", "garamond", "charis", "tiro")):
        return "sans-serif" if "sans" in f else "serif"
    return "sans-serif"


def _align(lines: list[dict]) -> str:
    if len(lines) < 2:
        return "left"
    x0s = [ln["bbox"][0] for ln in lines]
    x1s = [ln["bbox"][2] for ln in lines[:-1]]
    mids = [(ln["bbox"][0] + ln["bbox"][2]) / 2 for ln in lines]
    if max(x0s) - min(x0s) < 2:
        return "justify" if x1s and max(x1s) - min(x1s) < 2 and len(lines) > 2 else "left"
    if max(mids) - min(mids) < 2:
        return "center"
    if max(ln["bbox"][2] for ln in lines) - min(ln["bbox"][2] for ln in lines) < 2:
        return "right"
    return "left"


def _join_lines(lines: list[dict]) -> str:
    out = ""
    for ln in lines:
        t = "".join(s["text"] for s in ln["spans"]).strip()
        if not t:
            continue
        if out.endswith("-") and t[:1].islower():
            out = out[:-1] + t          # de-hyphenate "trans-\nlation"
        else:
            out = f"{out} {t}" if out else t
    return re.sub(r"\s+", " ", out)


def _cells(lines: list[dict]) -> list[dict]:
    """Split lines into cells at horizontal gaps wider than ~1 em (table columns, tab stops)."""
    cells = []
    for ln in lines:
        cur: list[dict] = []
        for s in ln["spans"]:
            if not s["text"].strip():
                continue
            if cur and s["bbox"][0] - cur[-1]["bbox"][2] > 0.9 * s["size"]:
                cells.append({"bbox": _union(cur), "dir": ln["dir"], "spans": cur})
                cur = []
            cur.append(s)
        if cur:
            cells.append({"bbox": _union(cur), "dir": ln["dir"], "spans": cur})
    return cells


def _union(spans: list[dict]) -> tuple:
    return (min(s["bbox"][0] for s in spans), min(s["bbox"][1] for s in spans),
            max(s["bbox"][2] for s in spans), max(s["bbox"][3] for s in spans))


def _tabular(lines: list[dict], cells: list[dict]) -> bool:
    """A block whose lines sit side by side, or whose lines contain wide gaps, is a table row / form row."""
    if len(cells) > len(lines):
        return True
    for a, b in zip(lines, lines[1:]):
        overlap = min(a["bbox"][3], b["bbox"][3]) - max(a["bbox"][1], b["bbox"][1])
        if overlap > 0.5 * min(a["bbox"][3] - a["bbox"][1], b["bbox"][3] - b["bbox"][1]):
            return True
    return False


def _segment(page: pymupdf.Page, sid: str, bbox: tuple, lines: list[dict]) -> Segment:
    spans = [s for ln in lines for s in ln["spans"] if s["text"].strip()]
    main: Counter = Counter()
    for s in spans:
        main[(s["font"], round(s["size"], 1), s["color"], s["flags"])] += len(s["text"])
    font, size, color, flags = main.most_common(1)[0][0]
    text = _join_lines(lines)
    horizontal = all(abs(ln["dir"][0] - 1) < 1e-3 and abs(ln["dir"][1]) < 1e-3 for ln in lines)
    seg = Segment(id=sid, page=page.number, bbox=tuple(float(v) for v in bbox), text=text, size=float(size),
                  color=_hex(color), bold=bool(flags & 16), italic=bool(flags & 2), family=_family(font, flags),
                  align=_align(lines), invisible=all(s.get("alpha", 255) == 0 for s in spans), lines=len(lines))
    seg.translate = horizontal and len(text) > 1 and not _NO_LETTERS.match(text) and not _URLISH.match(text)
    return seg


def segments(doc: pymupdf.Document, max_pages: int) -> list[Segment]:
    """One segment per text block (a paragraph is re-flowed as a whole), except table/form rows, which are split
    into one segment per cell so columns stay where they are."""
    if doc.page_count > max_pages:
        raise ValueError(f"Translation takes up to {max_pages} pages; this PDF has {doc.page_count}")
    segs: list[Segment] = []
    for page in doc:
        data = page.get_text("dict", flags=_FLAGS)
        for bno, b in enumerate(data["blocks"]):
            if b["type"] != 0:
                continue
            lines = [ln for ln in b["lines"] if any(s["text"].strip() for s in ln["spans"])]
            if not lines:
                continue
            cells = _cells(lines)
            if _tabular(lines, cells):
                for cno, c in enumerate(cells):
                    segs.append(_segment(page, f"p{page.number + 1}b{bno}c{cno}", c["bbox"], [c]))
            else:
                segs.append(_segment(page, f"p{page.number + 1}b{bno}", b["bbox"], lines))
    _mark_running_heads(segs, doc)
    for s in segs:
        s.key = template(s.text)[0] if s.role != "body" else s.text
    return segs


def extract_segments(path: str, password: str | None, max_pages: int) -> dict:
    """Sandbox entry point: translation segments plus the page texts (for the cached terminology context)."""
    doc = open_pdf(path, password)
    if isinstance(doc, dict):
        return doc
    try:
        if doc.page_count == 0:
            return _fail("broken", "This PDF has no pages")
        if doc.page_count > max_pages:
            return _fail("too_many_pages", f"The AI tools take up to {max_pages} pages; this PDF has "
                                           f"{doc.page_count}")
        segs = segments(doc, max_pages)
        return {"ok": True, "segments": segs, "pages": page_texts(doc, max_pages), "page_count": doc.page_count,
                "page_sizes": [(p.rect.width, p.rect.height) for p in doc]}
    except Exception:  # noqa: BLE001
        return _fail("broken", "This PDF is damaged and its text cannot be read (try Repair PDF first)")
    finally:
        doc.close()


def template(text: str) -> tuple[str, list[str]]:
    """'Page 3 of 8' -> ('Page ⟦1⟧ of ⟦2⟧', ['3', '8']) so a running head is translated once for all pages."""
    nums: list[str] = []

    def sub(m: re.Match) -> str:
        nums.append(m.group(0))
        return f"⟦{len(nums)}⟧"

    return re.sub(r"\d+", sub, text), nums


def fill_template(text: str, nums: list[str]) -> str:
    for i, n in enumerate(nums, 1):
        text = text.replace(f"⟦{i}⟧", n)
    return text


def _zone(s: Segment, height: float) -> str | None:
    return "header" if s.bbox[1] < height * 0.1 else "footer" if s.bbox[3] > height * 0.9 else None


def _mark_running_heads(segs: list[Segment], doc: pymupdf.Document) -> None:
    """Text repeated (digits ignored, same size) near the top/bottom of >= 3 pages or half the pages is a running
    header/footer. The size is part of the key, so a big title that repeats the running head stays body text."""
    if doc.page_count < 3:
        return
    heights = [p.rect.height for p in doc]

    def key(s: Segment, zone: str) -> tuple:
        return zone, re.sub(r"\d+", "#", s.text), round(s.size)

    counts: Counter = Counter()
    for s in segs:
        zone = _zone(s, heights[s.page])
        if zone:
            counts[key(s, zone)] += 1
    need = max(3, doc.page_count // 2)
    for s in segs:
        zone = _zone(s, heights[s.page])
        if zone and counts[key(s, zone)] >= need:
            s.role = zone
