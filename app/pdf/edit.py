"""Edit engines: page numbers, watermark, crop and the overlay editor (text, images, shapes, drawing).

All placement happens in visible-page space (rotation and CropBox applied) via :func:`util.upright`,
so the result lands where the user put it on the rendered page, whatever the page geometry.
"""
from __future__ import annotations

import io
import math
from pathlib import Path

import pymupdf

from app.pdf import fonts
from app.pdf import options as O
from app.pdf.errors import PdfError
from app.pdf.util import (Result, Src, clean_image, color, css_color, decode_data_url, has_signatures, open_pdf,
                          out_name, parse_pages, save_pdf, upright)

MAX_PAGES = 5000
MARGINS = {"small": 18.0, "medium": 30.0, "big": 48.0}
MAX_TILES = 400


def _pdf_srcs(srcs: list[Src]) -> list[Src]:
    out = [s for s in srcs if s.kind == "pdf"]
    if not out:
        raise PdfError(422, "Choose a PDF")
    return out


def _sig_warn(doc: pymupdf.Document, src: Src, res: Result) -> None:
    if has_signatures(doc):
        res.warn(f"“{src.name}” is digitally signed; the edited copy no longer carries a valid signature")


def _font(font_id: str, res: Result, *, bold: bool = False) -> fonts.Font:
    f = fonts.resolve(font_id, bold=bold)
    if f.missing:
        res.warn(f"The font “{font_id}” is not installed on this server; a standard font was used")
    return f


# ------------------------------------------------------------------ page numbers
def _roman(n: int) -> str:
    out = ""
    for v, s in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"),
                 (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= v:
            out += s
            n -= v
    return out


def _alpha(n: int) -> str:
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(97 + r) + out
    return out


def format_number(fmt: str, n: int, total: int) -> str:
    return (fmt.replace("{n}", str(n)).replace("{page}", str(n)).replace("{total}", str(total))
            .replace("{roman}", _roman(n)).replace("{ROMAN}", _roman(n).upper()).replace("{alpha}", _alpha(n)))


def page_numbers(srcs: list[Src], opts: O.PageNumbersOptions, out_dir: str) -> Result:
    res = Result()
    font = _font(opts.font, res, bold=opts.bold)
    col = css_color(opts.color)
    margin = MARGINS[opts.margin]
    for i, src in enumerate(_pdf_srcs(srcs)):
        doc = open_pdf(src, max_pages=MAX_PAGES, result=res)
        _sig_warn(doc, src, res)
        pages = parse_pages(opts.pages, doc.page_count)
        total = opts.start + len(pages) - 1
        vpos, hpos = opts.position.split("-")
        line_h = opts.size * 1.5
        for k, p in enumerate(pages):
            text = format_number(opts.format, opts.start + k, total)
            page = doc[p - 1]
            h = hpos
            if opts.facing and h in ("left", "right"):
                right_hand = (p % 2 == 1) if opts.first_cover else (p % 2 == 0)
                if not right_hand:
                    h = "left" if h == "right" else "right"
            with upright(page) as g:
                W, H = g.width, g.height
                m = min(margin, W / 4, H / 4)
                y0 = {"top": m, "middle": (H - line_h) / 2, "bottom": H - m - line_h}[vpos]
                vis = pymupdf.Rect(m, max(0, y0), W - m, min(H, y0 + line_h))
                fonts.text_box(page, g.rect_to_draw(vis), text, font, opts.size, col, align=h, bold=opts.bold,
                               rotate=g.rot)
        path = Path(out_dir) / f"numbered-{i:03d}.pdf"
        save_pdf(doc, path)
        res.add(path, out_name(src.name, "numbered", "pdf"))
    return res


# ------------------------------------------------------------------ watermark
def _image_bytes(srcs: list[Src], image_id: str | None, image_data: str | None) -> bytes:
    if image_data:
        return decode_data_url(image_data)
    for s in srcs:
        if s.id == image_id:
            if s.kind != "image":
                raise PdfError(422, f"“{s.name}” is not an image")
            try:
                return Path(s.path).read_bytes()
            except OSError:
                raise PdfError(404, f"“{s.name}” is no longer available") from None
    raise PdfError(422, "The chosen image was not sent with this job; add it to the files")


def _with_opacity(png_or_jpeg: bytes, opacity: float) -> bytes:
    """Bake opacity into the image's alpha channel (PDF image placement has no opacity of its own)."""
    if opacity >= 0.999:
        return png_or_jpeg
    from PIL import Image

    with Image.open(io.BytesIO(png_or_jpeg)) as im:
        rgba = im.convert("RGBA")
    alpha = rgba.getchannel("A").point(lambda a: int(a * max(0.0, opacity)))
    rgba.putalpha(alpha)
    buf = io.BytesIO()
    rgba.save(buf, "PNG")
    return buf.getvalue()


def _image_stamp(data: bytes, opacity: float) -> tuple[pymupdf.Document, float, float]:
    clean, w, h = clean_image(data)
    clean = _with_opacity(clean, opacity)
    doc = pymupdf.open()
    page = doc.new_page(width=w, height=h)
    page.insert_image(page.rect, stream=clean)
    return doc, float(w), float(h)


def _rotated_size(w: float, h: float, angle: float) -> tuple[float, float]:
    a = math.radians(angle)
    return abs(w * math.cos(a)) + abs(h * math.sin(a)), abs(w * math.sin(a)) + abs(h * math.cos(a))


def watermark(srcs: list[Src], opts: O.WatermarkOptions, out_dir: str) -> Result:
    res = Result()
    if opts.kind == "text":
        font = _font(opts.font, res, bold=opts.bold)
        stamp, srect = fonts.stamp(opts.text, font, opts.size, css_color(opts.color), bold=opts.bold,
                                   italic=opts.italic, opacity=opts.opacity)
        base_w, base_h = srect.width, srect.height
    else:
        stamp, base_w, base_h = _image_stamp(_image_bytes(srcs, opts.image_id, opts.image_data), opts.opacity)
    overlay = opts.layer == "over"
    for i, src in enumerate(_pdf_srcs(srcs)):
        doc = open_pdf(src, max_pages=MAX_PAGES, result=res)
        _sig_warn(doc, src, res)
        for p in parse_pages(opts.pages, doc.page_count):
            page = doc[p - 1]
            with upright(page) as g:
                W, H = g.width, g.height
                if opts.kind == "image":
                    w = opts.image_scale * W
                    h = w * base_h / base_w
                else:
                    w, h = base_w, base_h
                rw, rh = _rotated_size(w, h, opts.rotation)
                fit = min(1.0, (W - 2 * min(opts.margin, W / 4)) / max(rw, 1e-6),
                          (H - 2 * min(opts.margin, H / 4)) / max(rh, 1e-6))
                if fit < 1.0 and not opts.mosaic:
                    rw, rh = rw * fit, rh * fit
                for cx, cy in _anchors(opts, W, H, rw, rh):
                    vis = pymupdf.Rect(cx - rw / 2, cy - rh / 2, cx + rw / 2, cy + rh / 2)
                    page.show_pdf_page(g.rect_to_draw(vis), stamp, 0, keep_proportion=True, overlay=overlay,
                                       rotate=(g.rot + opts.rotation) % 360)
        path = Path(out_dir) / f"watermarked-{i:03d}.pdf"
        save_pdf(doc, path)
        res.add(path, out_name(src.name, "watermark", "pdf"))
    if not overlay:
        res.warn("A watermark below the content is hidden wherever the page has an opaque image or background")
    return res


def _anchors(opts: O.WatermarkOptions, W: float, H: float, rw: float, rh: float) -> list[tuple[float, float]]:
    if opts.mosaic:
        sx, sy = max(rw * 1.5, 24.0), max(rh * 2.0, 24.0)
        cols, rows = int(W / sx) + 2, int(H / sy) + 2
        while cols * rows > MAX_TILES:
            sx, sy = sx * 1.25, sy * 1.25
            cols, rows = int(W / sx) + 2, int(H / sy) + 2
        out = []
        for r in range(rows):
            off = (sx / 2) if r % 2 else 0.0
            for c in range(cols):
                out.append((c * sx + off, r * sy + sy / 2))
        return out
    m = min(opts.margin, W / 4, H / 4)
    vpos, hpos = opts.position.split("-")
    cx = {"left": m + rw / 2, "center": W / 2, "right": W - m - rw / 2}[hpos]
    cy = {"top": m + rh / 2, "middle": H / 2, "bottom": H - m - rh / 2}[vpos]
    return [(cx, cy)]


# ------------------------------------------------------------------ crop
def crop(srcs: list[Src], opts: O.CropOptions, out_dir: str) -> Result:
    res = Result()
    src = _pdf_srcs(srcs)[0]
    doc = open_pdf(src, max_pages=MAX_PAGES, result=res)
    _sig_warn(doc, src, res)
    changed = 0
    for p in parse_pages(opts.pages, doc.page_count):
        page = doc[p - 1]
        new_crop = None
        with upright(page) as g:
            full = pymupdf.Rect(0, 0, page.rect.width, page.rect.height)  # upright: MediaBox in drawing space
            if opts.mode == "box":
                assert opts.box is not None
                r = g.frac_to_draw(opts.box.x, opts.box.y, opts.box.w, opts.box.h)
            else:
                r = pymupdf.Rect()
                for _kind, bb in page.get_bboxlog():
                    bb = pymupdf.Rect(bb) & g.c
                    if not bb.is_empty and not bb.is_infinite:
                        r |= bb
                if r.is_empty:
                    res.warn(f"Page {p} is blank; it was left as it is")
                    continue
                r = r + (-opts.padding, -opts.padding, opts.padding, opts.padding)
            r = r & full
            if r.is_empty or r.width < 9 or r.height < 9:
                res.warn(f"The crop box on page {p} is too small; the page was left as it is")
                continue
            if opts.remove_outside:
                for area in (pymupdf.Rect(full.x0, full.y0, full.x1, r.y0), pymupdf.Rect(full.x0, r.y1, full.x1, full.y1),
                             pymupdf.Rect(full.x0, r.y0, r.x0, r.y1), pymupdf.Rect(r.x1, r.y0, full.x1, r.y1)):
                    if area.width > 0.01 and area.height > 0.01:
                        page.add_redact_annot(area, fill=False)
                page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                                      graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                                      text=pymupdf.PDF_REDACT_TEXT_REMOVE)
            new_crop = g.crop_from_draw(r)
        if new_crop is not None:
            page.set_cropbox(new_crop)
            changed += 1
    if not changed:
        raise PdfError(422, "Nothing was cropped; check the crop box")
    path = Path(out_dir) / "cropped.pdf"
    save_pdf(doc, path, garbage=4 if opts.remove_outside else 3)
    res.add(path, out_name(src.name, "crop", "pdf"))
    res.data.update(pages_cropped=changed)
    return res


# ------------------------------------------------------------------ overlay editor
def _arrow_head(tip: pymupdf.Point, tail: pymupdf.Point, size: float) -> list[pymupdf.Point]:
    ang = math.atan2(tip.y - tail.y, tip.x - tail.x)
    left = pymupdf.Point(tip.x - size * math.cos(ang - 0.45), tip.y - size * math.sin(ang - 0.45))
    right = pymupdf.Point(tip.x - size * math.cos(ang + 0.45), tip.y - size * math.sin(ang + 0.45))
    return [tip, left, right, tip]


def edit(srcs: list[Src], opts: O.EditOptions, out_dir: str) -> Result:
    res = Result()
    src = _pdf_srcs(srcs)[0]
    doc = open_pdf(src, max_pages=MAX_PAGES, result=res)
    _sig_warn(doc, src, res)
    by_page: dict[int, list] = {}
    for item in opts.items:
        if item.page > doc.page_count:
            raise PdfError(422, f"An item is on page {item.page}, but the document has {doc.page_count} pages")
        by_page.setdefault(item.page, []).append(item)
    images: dict[str, bytes] = {}
    for p, items in sorted(by_page.items()):
        page = doc[p - 1]
        with upright(page) as g:
            for item in items:
                if item.type == "text":
                    rect = g.frac_to_draw(item.x, item.y, item.w, item.h)
                    if item.background:
                        page.draw_rect(rect, color=None, fill=color(item.background), fill_opacity=item.opacity)
                    font = _font(item.font, res, bold=item.bold)
                    if not fonts.text_box(page, rect, item.text, font, item.size, css_color(item.color),
                                          align=item.align, bold=item.bold, italic=item.italic,
                                          opacity=item.opacity, rotate=g.rot):
                        res.warn(f"Some text on page {p} did not fit its box and was shrunk")
                elif item.type == "image":
                    key = item.image_id or f"data:{hash(item.image_data)}"
                    if key not in images:
                        clean, _w, _h = clean_image(_image_bytes(srcs, item.image_id, item.image_data))
                        images[key] = clean
                    data = _with_opacity(images[key], item.opacity)
                    page.insert_image(g.frac_to_draw(item.x, item.y, item.w, item.h), stream=data,
                                      keep_proportion=item.keep_ratio, rotate=(g.rot - item.rotation) % 360)
                elif item.type == "highlight":
                    # a real highlight annotation: MuPDF draws it with /BM /Multiply, so the text under it
                    # stays dark at any opacity — as the editor previews it (mix-blend-mode: multiply)
                    rect = g.frac_to_draw(item.x, item.y, item.w, item.h)
                    annot = page.add_highlight_annot(rect)
                    annot.set_colors(stroke=color(item.fill) or (1.0, 0.92, 0.23))
                    annot.set_opacity(item.opacity)
                    annot.set_flags(pymupdf.PDF_ANNOT_IS_PRINT)
                    annot.update()
                elif item.type in ("rect", "ellipse", "whiteout"):
                    rect = g.frac_to_draw(item.x, item.y, item.w, item.h)
                    stroke, fill, width = color(item.stroke), color(item.fill), item.width
                    if item.type == "whiteout":
                        stroke, fill, width = None, fill or (1, 1, 1), 0
                    shape = page.new_shape()
                    (shape.draw_oval if item.type == "ellipse" else shape.draw_rect)(rect)
                    shape.finish(color=stroke if width > 0 else None, fill=fill, width=width,
                                 stroke_opacity=item.opacity, fill_opacity=item.opacity)
                    shape.commit()
                elif item.type in ("line", "arrow"):
                    (x0, y0), (x1, y1) = item.points
                    a = g.to_draw(x0 * g.width, y0 * g.height)
                    b = g.to_draw(x1 * g.width, y1 * g.height)
                    shape = page.new_shape()
                    shape.draw_line(a, b)
                    shape.finish(color=color(item.stroke), width=item.width, stroke_opacity=item.opacity,
                                 lineCap=1)
                    if item.type == "arrow" and abs(a - b) > 1:
                        shape.draw_polyline(_arrow_head(b, a, max(6.0, item.width * 4)))
                        shape.finish(color=color(item.stroke), fill=color(item.stroke), width=item.width,
                                     stroke_opacity=item.opacity, fill_opacity=item.opacity, closePath=True)
                    shape.commit()
                elif item.type == "freehand":
                    shape = page.new_shape()
                    for stroke in item.strokes:
                        pts = [g.to_draw(x * g.width, y * g.height) for x, y in stroke]
                        shape.draw_polyline(pts)
                    shape.finish(color=color(item.color), width=item.width, closePath=False, lineCap=1,
                                 lineJoin=1, stroke_opacity=item.opacity)
                    shape.commit()
    path = Path(out_dir) / "edited.pdf"
    save_pdf(doc, path)
    res.add(path, out_name(src.name, "edited", "pdf"))
    res.data.update(items=len(opts.items), pages=sorted(by_page))
    return res
