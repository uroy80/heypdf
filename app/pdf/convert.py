"""Conversion engines: images → PDF, PDF → images / Word / Excel (light, in-app engines).

PDF → Markdown lives in :mod:`app.pdf.markdown`; Office/HTML/PowerPoint conversions belong to the docs
and renderer services.
"""
from __future__ import annotations

import contextlib
import io
import logging
import re
import unicodedata
from pathlib import Path

import pymupdf

from app.pdf import options as O
from app.pdf.errors import PdfError
from app.pdf.util import Result, Src, clean_image, open_pdf, out_name, parse_pages, save_pdf, upright

MAX_IMAGES = 200
MAX_PAGES_RENDER = 500
MAX_PAGE_PIXELS = 40_000_000
MAX_EXTRACTED = 5000
MAX_PAGES_WORD = 300
MAX_PAGES_EXCEL = 1000
PT_PER_MM = 72 / 25.4
PAGE_SIZES = {"a4": (210 * PT_PER_MM, 297 * PT_PER_MM), "letter": (612.0, 792.0)}


# ------------------------------------------------------------------ images → PDF
def _image_for_pdf(data: bytes) -> bytes:
    """JPEG/PNG bytes img2pdf can embed. Plain JPEGs pass through untouched (lossless); anything else
    (HEIC, WebP, alpha, CMYK, odd modes) is decoded, flattened onto white and re-encoded."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = 100_000_000
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt, mode, size = im.format, im.mode, im.size
            orientation = im.getexif().get(0x0112, 1) if fmt == "JPEG" else 1
    except Image.DecompressionBombError:
        raise PdfError(413, "An image has too many pixels") from None
    except Exception:
        fmt, mode, size, orientation = None, None, (0, 0), 1
    if size[0] * size[1] > 100_000_000:
        raise PdfError(413, "An image has too many pixels")
    if fmt == "JPEG" and mode in ("L", "RGB") and orientation in (1, None):
        return data
    cleaned, _w, _h = clean_image(data, max_side=12_000, max_pixels=100_000_000)
    with Image.open(io.BytesIO(cleaned)) as im:
        if im.mode in ("RGBA", "LA", "P"):
            rgba = im.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.getchannel("A"))
            buf = io.BytesIO()
            flat.save(buf, "PNG")
            return buf.getvalue()
    return cleaned


def _layout(opts: O.PageLayoutOptions):
    import img2pdf

    border = (float(opts.margin), float(opts.margin))
    if opts.page_size == "fit":
        return img2pdf.get_layout_fun(None, border=border if opts.margin else None)
    w, h = PAGE_SIZES[opts.page_size]
    if opts.orientation == "landscape":
        w, h = h, w
    return img2pdf.get_layout_fun((w, h), border=border, fit=img2pdf.FitMode.into,
                                  auto_orient=opts.orientation == "auto")


def images_to_pdf(images: list[bytes], names: list[str], opts: O.PageLayoutOptions, out_dir: str,
                  res: Result, *, suffix: str = "") -> None:
    import img2pdf

    layout = _layout(opts)
    groups = [list(range(len(images)))] if opts.merge else [[i] for i in range(len(images))]
    for g, idx in enumerate(groups):
        try:
            pdf = img2pdf.convert([images[i] for i in idx], layout_fun=layout,
                                  rotation=img2pdf.Rotation.ifvalid)
        except Exception:
            raise PdfError(422, "An image could not be converted to PDF") from None
        path = Path(out_dir) / f"images-{g:04d}.pdf"
        path.write_bytes(pdf)
        res.add(path, out_name(names[idx[0]], suffix, "pdf"))
    res.data.update(pages=len(images), files=len(groups))


def from_images(srcs: list[Src], opts: O.FromImagesOptions, out_dir: str) -> Result:
    res = Result()
    imgs = [s for s in srcs if s.kind == "image"]
    if not imgs:
        raise PdfError(422, "Choose at least one image")
    if len(imgs) > MAX_IMAGES:
        raise PdfError(413, f"At most {MAX_IMAGES} images per PDF")
    data = []
    for s in imgs:
        try:
            raw = Path(s.path).read_bytes()
        except OSError:
            raise PdfError(404, f"“{s.name}” is no longer available") from None
        try:
            data.append(_image_for_pdf(raw))
        except PdfError as exc:
            raise PdfError(exc.status, f"“{s.name}”: {exc.detail}") from None
    images_to_pdf(data, [s.name for s in imgs], opts, out_dir, res)
    return res


# ------------------------------------------------------------------ PDF → images
def to_images(srcs: list[Src], opts: O.ToImagesOptions, out_dir: str) -> Result:
    res = Result()
    count = 0
    for k, src in enumerate(srcs):
        doc = open_pdf(src, max_pages=5000, result=res)
        pages = parse_pages(opts.pages, doc.page_count)
        if opts.mode == "pages":
            if len(pages) > MAX_PAGES_RENDER:
                raise PdfError(413, f"At most {MAX_PAGES_RENDER} pages can be turned into images at once")
            width = max(4, len(str(doc.page_count)))
            for p in pages:
                page = doc[p - 1]
                dpi = opts.dpi
                w_in, h_in = page.rect.width / 72, page.rect.height / 72
                if w_in * h_in * dpi * dpi > MAX_PAGE_PIXELS:
                    dpi = max(9, int((MAX_PAGE_PIXELS / max(w_in * h_in, 1e-6)) ** 0.5))
                    res.warn(f"Page {p} is very large; it was rendered at {dpi} dpi")
                pix = page.get_pixmap(dpi=dpi, alpha=False)
                path = Path(out_dir) / f"{k:03d}-{p:05d}.{opts.format}"
                if opts.format == "jpg":
                    pix.save(str(path), output="jpg", jpg_quality=opts.quality)
                    mime = "image/jpeg"
                else:
                    pix.save(str(path), output="png")
                    mime = "image/png"
                res.add(path, out_name(src.name, f"page-{p:0{width}d}", opts.format), mime)
                count += 1
        else:
            seen: set[int] = set()
            n = 0
            for p in pages:
                for info in doc[p - 1].get_images(full=True):
                    xref, smask = info[0], info[1]
                    if xref in seen:
                        continue
                    seen.add(xref)
                    if info[2] < 16 or info[3] < 16:  # tiny decorations, masks
                        continue
                    n += 1
                    if count + n > MAX_EXTRACTED:
                        raise PdfError(413, f"The PDF holds more than {MAX_EXTRACTED} images")
                    data, ext = _extract(doc, xref, smask)
                    path = Path(out_dir) / f"{k:03d}-img-{n:05d}.{ext}"
                    path.write_bytes(data)
                    res.add(path, out_name(src.name, f"image-{n:03d}", ext),
                            "image/jpeg" if ext == "jpg" else "image/png")
            count += n
    if count == 0:
        raise PdfError(422, "No images were found in this PDF" if opts.mode == "extract" else "No pages selected")
    res.data.update(images=count)
    return res


def _extract(doc: pymupdf.Document, xref: int, smask: int) -> tuple[bytes, str]:
    """Original JPEG bytes when they can be used as-is, otherwise a PNG (with alpha if masked)."""
    try:
        info = doc.extract_image(xref)
    except Exception:
        info = None
    if info and not smask and info.get("ext") in ("jpeg", "jpg") and info.get("colorspace") in (1, 3):
        return info["image"], "jpg"
    try:
        pix = pymupdf.Pixmap(doc, xref)
        if smask:
            pix = pymupdf.Pixmap(pix, pymupdf.Pixmap(doc, smask))
        if pix.colorspace and pix.colorspace.n not in (1, 3):
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        return pix.tobytes("png"), "png"
    except Exception:
        if info:
            return info["image"], "png" if info.get("ext") == "png" else str(info.get("ext") or "bin")
        raise PdfError(422, "An embedded image could not be read") from None


# ------------------------------------------------------------------ PDF → Word
def _text_stats(doc: pymupdf.Document, pages: list[int]) -> tuple[int, int]:
    """(visible chars, invisible chars) — OCR layers are invisible (render mode 3)."""
    visible = invisible = 0
    for p in pages[:200]:
        try:
            for span in doc[p - 1].get_texttrace():
                n = len(span.get("chars", ()))
                if span.get("type") == 3 or span.get("opacity", 1) == 0:
                    invisible += n
                else:
                    visible += n
        except Exception:
            visible += len(doc[p - 1].get_text())
    return visible, invisible


def _docx_from_text(doc: pymupdf.Document, pages: list[int], path: Path) -> int:
    """Plain paragraphs from the text layer (also catches invisible OCR text pdf2docx ignores)."""
    import docx
    from docx.shared import Pt

    out = docx.Document()
    sizes: list[float] = []
    blocks_by_page = []
    for p in pages:
        blocks = []
        for b in doc[p - 1].get_text("dict", sort=True)["blocks"]:
            if b.get("type") != 0:
                continue
            lines = []
            bsize = 0.0
            for ln in b["lines"]:
                txt = "".join(s["text"] for s in ln["spans"]).strip()
                if txt:
                    lines.append(txt)
                    bsize = max(bsize, max(s["size"] for s in ln["spans"]))
            if lines:
                blocks.append((lines, bsize))
                sizes.append(bsize)
        blocks_by_page.append(blocks)
    body = sorted(sizes)[len(sizes) // 2] if sizes else 11
    paragraphs = 0
    for i, blocks in enumerate(blocks_by_page):
        if i:
            out.add_page_break()
        for lines, bsize in blocks:
            text = ""
            for ln in lines:
                if text.endswith("-") and ln[:1].islower():
                    text = text[:-1] + ln
                else:
                    text = f"{text} {ln}".strip()
            text = _xml_safe(text)
            if bsize > body * 1.3 and len(text) < 200:
                out.add_heading(text, level=1 if bsize > body * 1.7 else 2)
            else:
                para = out.add_paragraph(text)
                for run in para.runs:
                    run.font.size = Pt(min(max(body, 8), 14))
            paragraphs += 1
    out.save(str(path))
    return paragraphs


_XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _xml_safe(text: str) -> str:
    return _XML_BAD.sub("", text)


def _unrotate_sideways(doc: pymupdf.Document, pages: list[int]) -> list[int]:
    """pdf2docx drops pages with /Rotate 90 or 180 whose text runs left-to-right on the *unrotated*
    page (so it shows sideways or upside down). Turn those pages back to /Rotate 0 in this (working)
    copy; rotated pages whose text reads upright are converted fine and are left alone."""
    turned = []
    for p in pages:
        page = doc[p - 1]
        if page.rotation % 360 not in (90, 180):
            continue
        with upright(page):
            dirs = [ln.get("dir", (1, 0)) for b in page.get_text("dict").get("blocks", []) if b.get("type") == 0
                    for ln in b.get("lines", [])]
        horizontal = sum(1 for d in dirs if d[0] > 0.9)
        if dirs and horizontal * 2 >= len(dirs):
            page.set_rotation(0)
            turned.append(p)
    return turned


def _norm_words(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).lower().split())


def _pages_missing_from_docx(doc: pymupdf.Document, pages: list[int], path: Path) -> list[int]:
    """Pages with text none of whose three longest words made it into the DOCX (pdf2docx can silently
    produce nothing for a page)."""
    import docx

    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    body = docx.Document(str(path)).element.body
    # every paragraph (body, tables, text boxes): its runs' text joined as Word shows it
    parts = ["".join(t.text or "" for t in para.iter(f"{w}t")) for para in body.iter(f"{w}p")]
    have = _norm_words(" ".join(parts))
    per_page: dict[int, set[str]] = {}
    seen_on: dict[str, int] = {}
    for p in pages:
        words = {_norm_words(w[4].strip(".,;:!?()[]{}\"'“”‘’")) for w in doc[p - 1].get_text("words")}
        per_page[p] = {w for w in words if len(w) >= 4}
        for w in per_page[p]:
            seen_on[w] = seen_on.get(w, 0) + 1
    missing = []
    for p in pages:
        # the page's most distinctive words: on the fewest pages, then the longest
        probe = sorted(per_page[p], key=lambda w: (seen_on[w], -len(w), w))[:3]
        unique = [w for w in probe if seen_on[w] == 1]
        found = sum(1 for w in (unique or probe) if w in have)
        if probe and (found == 0 if unique else found < (len(probe) + 1) // 2):
            missing.append(p)
    return missing


def to_word(srcs: list[Src], opts: O.ToWordOptions, out_dir: str) -> Result:
    res = Result()
    src = srcs[0]
    doc = open_pdf(src, max_pages=MAX_PAGES_WORD, result=res)
    pages = parse_pages(opts.pages, doc.page_count)
    visible, invisible = _text_stats(doc, pages)
    mode = opts.mode
    if mode == "auto":
        mode = "text" if invisible > visible else "layout"
    if visible + invisible == 0:
        res.warn("This PDF has no text layer (it looks scanned). Run OCR PDF first to get editable text.")
    path = Path(out_dir) / "converted.docx"
    if mode == "text":
        n = _docx_from_text(doc, pages, path)
        res.data.update(mode="text", paragraphs=n)
    else:
        work = Path(out_dir) / "_input.pdf"
        turned = _unrotate_sideways(doc, pages)
        if turned or src.password or doc.is_encrypted:
            save_pdf(doc, work)  # decrypted / upright working copy for pdf2docx (the input is never changed)
        else:
            work = Path(src.path)
        from pdf2docx import Converter

        logging.disable(logging.WARNING)  # sandbox child only: pdf2docx logs every page at INFO

        try:
            cv = Converter(str(work))
            try:
                cv.convert(str(path), pages=[p - 1 for p in pages], multi_processing=False)
            finally:
                cv.close()
            missing = _pages_missing_from_docx(doc, pages, path)
        except PdfError:
            raise
        except Exception:
            # pdf2docx gives up on some layouts; fall back to plain paragraphs rather than failing
            res.warn("The layout could not be kept for this PDF; the text was converted as plain paragraphs")
            missing = None
        if missing is None or missing:
            if missing:
                shown = ", ".join(str(p) for p in missing[:10]) + (" …" if len(missing) > 10 else "")
                res.warn(f"The layout of page{'s' if len(missing) > 1 else ''} {shown} could not be kept, so the "
                         "whole document was converted as plain paragraphs")
            n = _docx_from_text(doc, pages, path)
            mode = "text"
            res.data.update(paragraphs=n)
        res.data.update(mode=mode)
    _fresh_app_properties(path)
    res.add(path, out_name(src.name, "", "docx"),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    return res


_APP_PROPERTIES = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                   b'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
                   b"<Application>heypdf</Application></Properties>")


def _fresh_app_properties(path: Path) -> None:
    """python-docx (and pdf2docx through it) copies its template's docProps/app.xml: "1 page, 0 words, written by
    Microsoft Word". Replace it with a part that claims nothing, so neither heypdf (FileInfo.pages) nor a word
    processor shows those numbers; Word and LibreOffice recount when they save."""
    import zipfile

    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = _APP_PROPERTIES if info.filename == "docProps/app.xml" else src.read(info)
            dst.writestr(info, data, compress_type=info.compress_type)
    tmp.replace(path)


# ------------------------------------------------------------------ PDF → Excel
_NUM = re.compile(r"^\(?[-+]?[\$€£₹¥]?\s?\d{1,3}(?:[,  ]\d{3})*(?:\.\d+)?\)?%?$|^\(?[-+]?[\$€£₹¥]?\s?\d+(?:\.\d+)?\)?%?$")


def to_number(value: str) -> float | int | str:
    """'1,234.50' → 1234.5, '(12)' → -12, '12%' → 0.12; leaves codes like '007' and anything else as text."""
    s = value.strip()
    if not s or not _NUM.match(s):
        return value
    digits = re.sub(r"[^\d.]", "", s)
    if len(digits) > 1 and digits.startswith("0") and not digits.startswith("0."):
        return value  # identifiers with leading zeros stay text
    negative = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    try:
        num = float(digits)
    except ValueError:
        return value
    if s.endswith("%") or s.endswith("%)"):
        num /= 100
    if negative:
        num = -num
    return int(num) if num.is_integer() and "." not in digits and not s.endswith("%") else num


def find_tables(page: pymupdf.Page, **kwargs):
    """``page.find_tables`` without its one-off advertising line on stdout (server logs)."""
    with contextlib.redirect_stdout(io.StringIO()):
        return page.find_tables(**kwargs)


MAX_GUESSED_COLUMNS = 15
MIN_WORDS_IN_ONE_CELL = 0.9
MAX_EMPTY_CELLS = 0.5


def _looks_like_a_table(table, rows: list[list[str]], words: list) -> bool:
    """Quality gate for tables guessed from whitespace (no ruling lines): prose, two-column layouts and
    OCR text layers also "align" — but their words straddle the guessed cell borders. Accept only a
    sane column count, mostly filled cells, and ≥ 90 % of the words inside the table lying wholly in
    one cell."""
    ncols = max((len(r) for r in rows), default=0)
    if ncols > MAX_GUESSED_COLUMNS:
        return False
    total = sum(len(r) for r in rows)
    if total == 0 or sum(1 for r in rows for c in r if not c) / total >= MAX_EMPTY_CELLS:
        return False
    cells = [pymupdf.Rect(c) for c in (table.cells or []) if c]
    box = pymupdf.Rect(table.bbox)
    inside = [pymupdf.Rect(w[:4]) for w in words
              if box.contains(pymupdf.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2))]
    if not inside or not cells:
        return False

    def in_one_cell(w: pymupdf.Rect) -> bool:
        # across: the whole word; down: its middle (word boxes are taller than the guessed rows)
        cy = (w.y0 + w.y1) / 2
        return any(c.x0 - 1 <= w.x0 and w.x1 <= c.x1 + 1 and c.y0 - 1 <= cy <= c.y1 + 1 for c in cells)

    whole = sum(1 for w in inside if in_one_cell(w))
    return whole / len(inside) >= MIN_WORDS_IN_ONE_CELL


def _tables(page: pymupdf.Page, strategy: str, *, gate: bool = False) -> list[list[list[str]]]:
    try:
        found = find_tables(page, strategy=strategy)
    except Exception:
        return []
    words = page.get_text("words") if gate else []
    out = []
    for t in found.tables:
        rows = [[(c or "").strip() for c in row] for row in t.extract()]
        rows = [r for r in rows if any(r)]
        if len(rows) >= 2 and max((len(r) for r in rows), default=0) >= 2:
            if gate and not _looks_like_a_table(t, rows, words):
                continue
            out.append(rows)
    return out


def to_excel(srcs: list[Src], opts: O.ToExcelOptions, out_dir: str) -> Result:
    import openpyxl
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    res = Result()
    src = srcs[0]
    doc = open_pdf(src, max_pages=MAX_PAGES_EXCEL, result=res)
    pages = parse_pages(opts.pages, doc.page_count)
    strategies = {"auto": ["lines"], "lines": ["lines"], "text": ["text"]}[opts.strategy]
    found: list[tuple[int, list[list[str]]]] = []
    for p in pages:
        for rows in _tables(doc[p - 1], strategies[0]):
            found.append((p, rows))
    if not found and opts.strategy == "auto":  # borderless tables (bank statements …), quality-gated
        for p in pages:
            for rows in _tables(doc[p - 1], "text", gate=True):
                found.append((p, rows))
    if not found:
        has_text = any(doc[p - 1].get_text().strip() for p in pages[:50])
        raise PdfError(422, "No tables were found in this PDF" +
                       ("" if has_text else " — it looks scanned; run OCR PDF first"))
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    bold = Font(bold=True)

    def cell_value(text: str):
        text = ILLEGAL_CHARACTERS_RE.sub("", text)
        return to_number(text) if opts.numbers else text

    def write(ws, rows: list[list[str]], start_row: int, caption: str | None = None) -> int:
        r = start_row
        if caption:
            ws.cell(row=r, column=1, value=caption).font = bold
            r += 1
        for i, row in enumerate(rows):
            for j, text in enumerate(row):
                value = cell_value(text)
                cell = ws.cell(row=r, column=j + 1, value=value)
                if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
                    cell.data_type = "s"  # never let document text become a formula
                if isinstance(value, float) and (text.strip().endswith("%") or text.strip().endswith("%)")):
                    cell.number_format = "0.00%"
                if i == 0:
                    cell.font = bold
            r += 1
        return r

    def finish(ws) -> None:
        for col in ws.columns:
            width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
            ws.column_dimensions[get_column_letter(col[0].column)].width = min(60, max(8, width + 2))

    if opts.layout == "sheet_per_table":
        counters: dict[int, int] = {}
        for p, rows in found:
            counters[p] = counters.get(p, 0) + 1
            ws = wb.create_sheet(f"Page {p} table {counters[p]}"[:31])
            write(ws, rows, 1)
            ws.freeze_panes = "A2"
            finish(ws)
    elif opts.layout == "sheet_per_page":
        by_page: dict[int, list[list[list[str]]]] = {}
        for p, rows in found:
            by_page.setdefault(p, []).append(rows)
        for p, tables in by_page.items():
            ws = wb.create_sheet(f"Page {p}"[:31])
            r = 1
            for rows in tables:
                r = write(ws, rows, r) + 1
            finish(ws)
    else:
        ws = wb.create_sheet("Tables")
        r = 1
        counters = {}
        for p, rows in found:
            counters[p] = counters.get(p, 0) + 1
            r = write(ws, rows, r, caption=f"Page {p}, table {counters[p]}") + 1
        finish(ws)
    path = Path(out_dir) / "converted.xlsx"
    wb.save(str(path))
    res.add(path, out_name(src.name, "", "xlsx"), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    res.data.update(tables=len(found), pages_with_tables=len({p for p, _ in found}))
    return res
