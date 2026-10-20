"""Shared helpers: opening/saving PDFs safely, page ranges, colours, names and visible-page geometry.

Geometry: PyMuPDF's own rotation matrices disagree with its drawing calls on pages that have both a
/Rotate and an offset CropBox (verified on 1.28.2). Every geometric operation therefore runs inside
:func:`upright`, which temporarily sets /Rotate 0 and CropBox = MediaBox so drawing coordinates are
plain "MediaBox, top-left origin" coordinates, and :class:`Geo` maps between those and the *visible*
page the user sees (the rotated CropBox). This was checked pixel-exact for all four rotations with
offset CropBoxes and MediaBoxes not anchored at the origin.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import io
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from app.pdf.errors import PdfError, password_error

MAGIC_WINDOW = 1024
MAX_RANGE_SPEC = 2000
_RANGE_TOKEN = re.compile(r"^(?:(\d{1,6}|first|last|end)?-(\d{1,6}|first|last|end)?|(\d{1,6}|first|last|end))$")


# ------------------------------------------------------------------ job protocol (picklable)
@dataclass(frozen=True)
class Src:
    """One job input as the engine sees it."""

    id: str
    name: str
    path: str
    kind: str = "pdf"
    password: str | None = None


@dataclass
class Out:
    path: str
    name: str
    mime: str


@dataclass
class Result:
    """What an engine function returns to the tool wrapper."""

    outputs: list[Out] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)

    def add(self, path: str | Path, name: str, mime: str = "application/pdf") -> None:
        self.outputs.append(Out(str(path), name, mime))

    def warn(self, msg: str) -> None:
        if msg not in self.warnings and len(self.warnings) < 50:
            self.warnings.append(msg)


# ------------------------------------------------------------------ files
def check_magic(path: str | Path, name: str) -> None:
    """Refuse anything that does not start like a PDF (critic: never hand PostScript to an engine)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(MAGIC_WINDOW)
    except OSError:
        raise PdfError(404, f"“{name}” is no longer available") from None
    if b"%PDF-" not in head:
        raise PdfError(415, f"“{name}” is not a PDF file")


def open_pdf(src: Src, *, max_pages: int | None = None, result: Result | None = None) -> pymupdf.Document:
    """Open ``src`` with MuPDF, authenticating with its password. Raises PdfError (422/413)."""
    check_magic(src.path, src.name)
    try:
        doc = pymupdf.open(src.path, filetype="pdf")
    except Exception:
        raise PdfError(422, f"“{src.name}” is damaged and could not be opened") from None
    if doc.needs_pass:
        if not src.password:
            raise password_error(src.name, wrong=False, file_id=src.id)
        if not doc.authenticate(src.password):
            raise password_error(src.name, wrong=True, file_id=src.id)
    if doc.page_count < 1:
        raise PdfError(422, f"“{src.name}” has no pages")
    if doc.is_repaired:
        try:  # a repaired page tree may still hold non-page objects: fail cleanly, not mid-operation
            for page in doc:
                _ = page.rotation, page.rect
        except Exception:
            raise PdfError(422, f"“{src.name}” is damaged; try Repair PDF first") from None
    if max_pages is not None and doc.page_count > max_pages:
        raise PdfError(413, f"“{src.name}” has {doc.page_count} pages; this tool handles up to {max_pages}")
    if result is not None:
        if doc.is_repaired:
            result.warn(f"“{src.name}” was damaged and has been repaired automatically; check the output")
        if doc.is_encrypted or src.password:
            result.warn(f"The password protection of “{src.name}” is not carried over to the output; "
                        "use Protect PDF to add it again")
    return doc


def has_signatures(doc: pymupdf.Document) -> bool:
    try:
        return any(w.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE
                   for page in doc for w in (page.widgets() or []))
    except Exception:
        return False


def save_pdf(doc: pymupdf.Document, path: str | Path, *, garbage: int = 3, clean: bool = False) -> None:
    """Write a compact, unencrypted copy (unused objects removed, streams compressed)."""
    try:
        doc.save(str(path), garbage=garbage, deflate=True, clean=clean, use_objstms=1)
    except Exception:
        # some damaged files refuse object streams / garbage collection; fall back to a plain write
        doc.save(str(path), garbage=1, deflate=True)


def stem(name: str) -> str:
    base, dot, ext = name.rpartition(".")
    s = base if dot and base and len(ext) <= 5 else name
    s = re.sub(r"[\x00-\x1f/\\]", "_", s).strip() or "document"
    return s[:100]


def out_name(name: str, suffix: str, ext: str) -> str:
    """``<stem>_<suffix>.<ext>`` (or ``<stem>.<ext>`` without a suffix)."""
    s = stem(name)
    return f"{s}_{suffix}.{ext}" if suffix else f"{s}.{ext}"


# ------------------------------------------------------------------ page ranges (1-based, inclusive)
def _normalise(spec: str) -> str:
    spec = (spec or "").strip().lower()
    if len(spec) > MAX_RANGE_SPEC:
        raise ValueError("page list is too long")
    return re.sub(r"\s*-\s*", "-", spec)


def _items(spec: str) -> list[str]:
    return [t for t in re.split(r"[,;\s]+", spec) if t]


def check_range_syntax(spec: str) -> str:
    """Validate the grammar only (page counts are checked once the document is open).

    Items are separated by commas or spaces: ``all``, ``odd``, ``even``, ``N``, ``N-M``, ``N-`` (to the
    end), ``-M`` (from the start); ``first``, ``last`` and ``end`` may stand for numbers.
    """
    spec = _normalise(spec)
    for item in _items(spec):
        if item not in ("all", "odd", "even") and not _RANGE_TOKEN.match(item):
            raise ValueError(f"cannot read page range {item!r}; use e.g. 1-3, 5, 8-last")
    return spec


def _num(tok: str | None, count: int, default: int) -> int:
    if tok is None or tok == "":
        return default
    if tok == "first":
        return 1
    if tok in ("last", "end"):
        return count
    return int(tok)


def parse_ranges(spec: str, count: int, *, allow_reverse: bool = False) -> list[tuple[int, int]]:
    """Ranges as ``(first, last)`` 1-based pairs in the order given; ``all``/``odd``/``even`` expand."""
    spec = check_range_syntax(spec)
    items = _items(spec)
    if not items:
        return [(1, count)]
    out: list[tuple[int, int]] = []
    for item in items:
        if item == "all":
            out.append((1, count))
        elif item in ("odd", "even"):
            out.extend((p, p) for p in range(1 if item == "odd" else 2, count + 1, 2))
        else:
            m = _RANGE_TOKEN.match(item)
            assert m is not None  # checked above
            if m.group(3) is not None:
                a = b = _num(m.group(3), count, 1)
            else:
                a, b = _num(m.group(1), count, 1), _num(m.group(2), count, count)
            for p in (a, b):
                if p < 1 or p > count:
                    raise ValueError(f"page {p} does not exist (the document has {count} page"
                                     f"{'s' if count != 1 else ''})")
            if a > b and not allow_reverse:
                raise ValueError(f"page range {a}-{b} runs backwards")
            out.append((a, b))
        if len(out) > 100_000:
            raise ValueError("too many page ranges")
    return out


def parse_pages(spec: str, count: int, *, empty_means_all: bool = True) -> list[int]:
    """Sorted, de-duplicated 1-based page numbers selected by ``spec``."""
    if not _items(_normalise(spec)):
        if empty_means_all:
            return list(range(1, count + 1))
        raise ValueError("choose at least one page")
    pages: set[int] = set()
    for a, b in parse_ranges(spec, count):
        pages.update(range(a, b + 1))
    return sorted(pages)


# ------------------------------------------------------------------ colours
def color(hex_value: str | None) -> tuple[float, float, float] | None:
    if hex_value is None:
        return None
    h = hex_value.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        raise ValueError(f"invalid colour {hex_value!r}")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def css_color(hex_value: str) -> str:
    r, g, b = color(hex_value) or (0, 0, 0)
    return f"rgb({round(r * 255)},{round(g * 255)},{round(b * 255)})"


# ------------------------------------------------------------------ geometry
class Geo:
    """Maps the visible page (rotated CropBox, top-left origin) to drawing coordinates in the upright page."""

    def __init__(self, rotation: int, crop: pymupdf.Rect, mediabox: pymupdf.Rect) -> None:
        self.rot = int(rotation) % 360
        # crop as reported by PyMuPDF: x absolute, y measured down from the MediaBox top
        self.c = pymupdf.Rect(crop.x0 - mediabox.x0, crop.y0, crop.x1 - mediabox.x0, crop.y1)
        self.mb_x0 = mediabox.x0
        self.width = self.c.width if self.rot in (0, 180) else self.c.height
        self.height = self.c.height if self.rot in (0, 180) else self.c.width

    # points
    def to_draw(self, u: float, v: float) -> pymupdf.Point:
        c = self.c
        x, y = {0: (c.x0 + u, c.y0 + v), 90: (c.x0 + v, c.y1 - u),
                180: (c.x1 - u, c.y1 - v), 270: (c.x1 - v, c.y0 + u)}[self.rot]
        return pymupdf.Point(x, y)

    def from_draw(self, x: float, y: float) -> pymupdf.Point:
        c = self.c
        u, v = {0: (x - c.x0, y - c.y0), 90: (c.y1 - y, x - c.x0),
                180: (c.x1 - x, c.y1 - y), 270: (y - c.y0, c.x1 - x)}[self.rot]
        return pymupdf.Point(u, v)

    # rects
    def rect_to_draw(self, r: pymupdf.Rect) -> pymupdf.Rect:
        a, b = self.to_draw(r.x0, r.y0), self.to_draw(r.x1, r.y1)
        return pymupdf.Rect(min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y))

    def rect_from_draw(self, r: Iterable[float]) -> pymupdf.Rect:
        x0, y0, x1, y1 = list(r)[:4]
        a, b = self.from_draw(x0, y0), self.from_draw(x1, y1)
        return pymupdf.Rect(min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y))

    def frac_rect(self, x: float, y: float, w: float, h: float) -> pymupdf.Rect:
        """Visible-page fractions → visible points."""
        return pymupdf.Rect(x * self.width, y * self.height, (x + w) * self.width, (y + h) * self.height)

    def frac_to_draw(self, x: float, y: float, w: float, h: float) -> pymupdf.Rect:
        return self.rect_to_draw(self.frac_rect(x, y, w, h))

    def draw_to_frac(self, r: Iterable[float]) -> dict:
        v = self.rect_from_draw(r)
        W, H = self.width or 1, self.height or 1
        return {"x": round(v.x0 / W, 5), "y": round(v.y0 / H, 5),
                "w": round(v.width / W, 5), "h": round(v.height / H, 5)}

    def crop_from_draw(self, r: pymupdf.Rect) -> pymupdf.Rect:
        """A drawing-space rect as a CropBox value for ``page.set_cropbox``."""
        return pymupdf.Rect(r.x0 + self.mb_x0, r.y0, r.x1 + self.mb_x0, r.y1)


@contextlib.contextmanager
def upright(page: pymupdf.Page) -> Iterator[Geo]:
    """Temporarily remove /Rotate and widen the CropBox to the MediaBox; restore them afterwards."""
    rot, crop, mb = page.rotation, page.cropbox, page.mediabox
    geo = Geo(rot, crop, mb)
    changed = rot != 0 or crop != pymupdf.Rect(mb.x0, 0, mb.x1, mb.height)
    if changed:
        page.set_rotation(0)
        page.set_cropbox(pymupdf.Rect(mb.x0, 0, mb.x1, mb.height))
    try:
        yield geo
    finally:
        if changed:
            page.set_cropbox(crop)
            page.set_rotation(rot)


# ------------------------------------------------------------------ images supplied in options / inputs
MAX_DATA_URL = 12 * 1024 * 1024
_DATA_URL = re.compile(r"^data:image/(png|jpeg|jpg|webp|gif);base64,", re.I)


def decode_data_url(value: str) -> bytes:
    m = _DATA_URL.match(value or "")
    if not m:
        raise PdfError(422, "The image must be a PNG, JPEG, WebP or GIF data URL")
    if len(value) > MAX_DATA_URL:
        raise PdfError(413, "The image is too large (max 8 MB)")
    try:
        return base64.b64decode(value[m.end():], validate=True)
    except (binascii.Error, ValueError):
        raise PdfError(422, "The image data is not valid base64") from None


def _pil_decode(data: bytes, max_pixels: int):
    """Plain Pillow decode (EXIF-oriented, 16/32-bit scaled to 8-bit) for images above the image reader's own pixel
    limit that a caller explicitly allows (e.g. large scans for JPG → PDF)."""
    import numpy as np
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = max_pixels
    with Image.open(io.BytesIO(data)) as im:
        if im.width * im.height > max_pixels:
            raise PdfError(413, "The image has too many pixels")
        im.load()
        img = ImageOps.exif_transpose(im)
    if img.mode.startswith("I;16") or img.mode in ("I", "F"):
        arr = np.asarray(img).astype(np.float64)
        scale = 257.0 if img.mode.startswith("I;16") or float(arr.max(initial=0)) > 255 else 1.0
        img = Image.fromarray(np.clip(np.rint(arr / scale), 0, 255).astype(np.uint8))
    return img


def load_image(data: bytes, *, max_pixels: int = 40_000_000, name: str | None = None):
    """Decode an untrusted image with the image reader (``app.imaging.io.load``: HEIC/AVIF, SVG, camera RAW,
    PSD, CMYK/ICC, 16-bit → 8-bit, EXIF orientation). Returns a PIL image in RGB, RGBA, L or LA.
    Raises PdfError 413 (too many pixels) or 415 (unreadable)."""
    from PIL import Image

    from app.imaging import io as imgio

    try:
        img = imgio.load(data, name=name).image
    except imgio.ImageError as exc:
        if exc.status != 413:
            raise PdfError(415, "The image could not be read") from None
        if max_pixels <= imgio.max_pixels():
            raise PdfError(413, "The image has too many pixels") from None
        try:  # above the image reader's decode limit but within what this caller allows
            img = _pil_decode(data, max_pixels)
        except PdfError:
            raise
        except Image.DecompressionBombError:
            raise PdfError(413, "The image has too many pixels") from None
        except Exception:
            raise PdfError(415, "The image could not be read") from None
    except MemoryError:
        raise PdfError(413, "The image has too many pixels") from None
    except Exception:
        raise PdfError(415, "The image could not be read") from None
    if img.width * img.height > max_pixels:
        raise PdfError(413, "The image has too many pixels")
    return img


def clean_image(data: bytes, *, max_side: int = 3000, max_pixels: int = 40_000_000) -> tuple[bytes, int, int]:
    """Decode an untrusted image (any format the image reader takes), orient it and re-encode as PNG (keeps alpha)
    or JPEG (opaque photos). Returns ``(bytes, width, height)``."""
    img = load_image(data, max_pixels=max_pixels)
    if img.width * img.height > max_pixels:
        raise PdfError(413, "The image has too many pixels")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side))
    has_alpha = img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info)
    buf = io.BytesIO()
    if has_alpha:
        img.convert("RGBA").save(buf, "PNG", optimize=False)
    elif img.mode in ("1", "L"):
        img.convert("L").save(buf, "PNG")
    else:
        img.convert("RGB").save(buf, "JPEG", quality=92)
    return buf.getvalue(), img.width, img.height
