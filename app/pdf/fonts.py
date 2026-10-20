"""Fonts and text placement for heypdf (page numbers, watermarks, stamps, signatures, edit text).

Font ids are the faces bundled in ``app/assets/fonts`` plus the three PDF base fonts. Text is laid
out with MuPDF's HTML engine (``insert_htmlbox``), which falls back to MuPDF's built-in Noto fonts for
scripts the chosen font lacks (Devanagari, Tamil, CJK, …), so any language renders.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pymupdf

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

FontId = Literal["helvetica", "times", "courier", "libre_franklin", "fraunces", "plex_mono", "dejavu_sans",
                 "dejavu_serif", "dejavu_mono", "anton", "caveat", "dancing_script", "great_vibes",
                 "homemade_apple"]
SignatureFontId = Literal["dancing_script", "great_vibes", "caveat", "homemade_apple"]

_BASE = {"helvetica": "sans-serif", "times": "serif", "courier": "monospace"}
# id -> (regular file, bold file or None)
_FILES: dict[str, tuple[str, str | None]] = {
    "libre_franklin": ("LibreFranklin-Variable.ttf", None),
    "fraunces": ("Fraunces-Variable.ttf", None),
    "plex_mono": ("IBMPlexMono-Regular.ttf", "IBMPlexMono-Bold.ttf"),
    "dejavu_sans": ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
    "dejavu_serif": ("DejaVuSerif.ttf", None),
    "dejavu_mono": ("DejaVuSansMono.ttf", None),
    "anton": ("Anton-Regular.ttf", None),
    "caveat": ("Caveat-Variable.ttf", None),
    "dancing_script": ("DancingScript-Variable.ttf", None),
    "great_vibes": ("GreatVibes-Regular.ttf", None),
    "homemade_apple": ("HomemadeApple-Regular.ttf", None),
}
_FALLBACK_FAMILY = {"dancing_script": "serif", "great_vibes": "serif", "caveat": "serif",
                    "homemade_apple": "serif", "fraunces": "serif", "dejavu_serif": "serif",
                    "plex_mono": "monospace", "dejavu_mono": "monospace"}


@dataclass(frozen=True)
class Font:
    id: str
    css: str               # @font-face rules ("" for base fonts)
    family: str            # CSS font-family value
    archive: str | None    # directory the @font-face url() is resolved against
    missing: bool = False  # bundled file absent → generic fallback in use


def font_file(font_id: str) -> Path | None:
    """The bundled TTF of a font id (regular face), or None for a base font, an unknown id or a missing file."""
    files = _FILES.get(font_id)
    path = FONT_DIR / files[0] if files else None
    return path if path is not None and path.is_file() else None


def resolve(font_id: str, *, bold: bool = False) -> Font:
    key = (font_id or "helvetica").lower().replace("-", "_")
    if key in _BASE:
        return Font(key, "", _BASE[key], None)
    files = _FILES.get(key)
    if files is None:
        return Font("helvetica", "", "sans-serif", None, missing=True)
    name = files[1] if bold and files[1] else files[0]
    if not (FONT_DIR / name).is_file():
        return Font(key, "", _FALLBACK_FAMILY.get(key, "sans-serif"), None, missing=True)
    face = f"@font-face {{font-family: hp_{key}; src: url({name});}}"
    return Font(key, face, f"hp_{key}", str(FONT_DIR))


def _html(text: str) -> str:
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    for ln in lines:
        esc = html.escape(ln, quote=False)
        # keep runs of spaces (HTML would collapse them)
        esc = esc.replace("  ", "  ")
        out.append(esc or " ")
    return "<br>".join(out)


def css(font: Font, size: float, color: str, *, align: str = "left", bold: bool = False,
        italic: bool = False, line_height: float = 1.2) -> str:
    return (f"{font.css} * {{margin:0; padding:0; font-family: {font.family}; font-size: {size:.2f}px; "
            f"line-height: {line_height}; color: {color}; text-align: {align}; "
            f"font-weight: {'bold' if bold else 'normal'}; font-style: {'italic' if italic else 'normal'};}}")


# Scripts whose shaped glyphs (conjuncts, ligatures, contextual forms) MuPDF's HTML engine writes with a
# ToUnicode map that does not round-trip: Hebrew/Arabic … Myanmar, Khmer/Mongolian, Devanagari Extended,
# Arabic presentation forms.
_COMPLEX = ((0x0590, 0x10FF), (0x1780, 0x18AF), (0xA8E0, 0xA8FF), (0xFB1D, 0xFDFF), (0xFE70, 0xFEFF))


def needs_text_layer(text: str) -> bool:
    return any(lo <= ord(c) <= hi for c in text or "" for lo, hi in _COMPLEX)


def _actual_text(text: str) -> bytes:
    return b"<FEFF" + text.encode("utf-16-be").hex().upper().encode("ascii") + b">"


def _text_carrier(text: str, width: float, height: float) -> pymupdf.Document:
    """A one-page PDF whose only content is one invisible glyph, stretched across the page and marked
    with ``/ActualText`` = ``text`` (so search hits cover the box, and a crop to the ink keeps it)."""
    doc = pymupdf.open()
    page = doc.new_page(width=max(1.0, width), height=max(1.0, height))
    # Helvetica's box is 1.075 em above and 0.299 em below the baseline: the glyph's box fills the page
    size = page.rect.height / 1.374
    page.insert_text((0, page.rect.height - 0.299 * size), ".", fontsize=size, render_mode=3, fontname="helv")
    xref = page.get_contents()[-1]
    stretch = 100.0 * page.rect.width / (0.278 * size)  # Helvetica's "." is 0.278 em wide
    data = doc.xref_stream(xref).replace(b"BT", b"BT %.2f Tz" % stretch, 1)
    doc.update_stream(xref, b"/Span <</ActualText " + _actual_text(text) + b">> BDC\n" + data + b"\nEMC\n")
    return doc


def _fix_text_layer(page: pymupdf.Page, before: set[int], rect: pymupdf.Rect, text: str, rotate: int,
                    overlay: bool) -> None:
    """Search and copy of complex-script text placed with ``insert_htmlbox``: its glyphs are shaped
    correctly but their ToUnicode is not, so they are marked with an empty /ActualText (MuPDF and other
    readers then extract nothing from them) and an invisible carrier glyph over the box gives the original
    string as /ActualText."""
    new = [x for x in page.get_xobjects() if x[0] not in before and x[2] == 0]
    if len(new) != 1:
        return  # not the layout we know: leave it as it is
    doc = page.parent
    xref = new[0][0]
    doc.update_stream(xref, b"/Span <</ActualText ()>> BDC\n" + doc.xref_stream(xref) + b"\nEMC\n")
    flat = " ".join(" ".join(ln.split()) for ln in text.splitlines() if ln.strip())
    w, h = (rect.height, rect.width) if rotate % 180 else (rect.width, rect.height)
    page.show_pdf_page(rect, _text_carrier(flat, w, h), 0, keep_proportion=False, overlay=overlay,
                       rotate=rotate)


def text_box(page: pymupdf.Page, rect: pymupdf.Rect, text: str, font: Font, size: float, color: str, *,
             align: str = "left", bold: bool = False, italic: bool = False, opacity: float = 1.0,
             rotate: int = 0, shrink: bool = True, overlay: bool = True, text_layer: bool = True) -> bool:
    """Lay ``text`` out inside ``rect`` (drawing coordinates). ``rotate`` (multiple of 90) turns the text
    so it reads upright on a page displayed with that /Rotate. Returns False if it did not fit.
    ``text_layer=False`` leaves out the search layer of complex scripts (the caller adds it)."""
    archive = pymupdf.Archive(font.archive) if font.archive else None
    fix = text_layer and needs_text_layer(text)
    before = {x[0] for x in page.get_xobjects()} if fix else set()
    spare, _scale = page.insert_htmlbox(
        rect, f"<div>{_html(text)}</div>", css=css(font, size, color, align=align, bold=bold, italic=italic),
        archive=archive, rotate=rotate, opacity=max(0.0, min(1.0, opacity)), overlay=overlay,
        scale_low=0 if shrink else 1)
    if fix and text.strip():
        try:
            _fix_text_layer(page, before, rect, text, rotate, overlay)
        except Exception:  # the visible text is already there; only search/copy would stay imperfect
            pass
    return spare >= 0


def stamp(text: str, font: Font, size: float, color: str, *, bold: bool = False, italic: bool = False,
          opacity: float = 1.0, align: str = "center") -> tuple[pymupdf.Document, pymupdf.Rect]:
    """A one-page PDF whose CropBox hugs ``text`` — place it anywhere, at any angle, with
    ``show_pdf_page``. Returns the document and the text's size as a Rect at the origin."""
    lines = (text or " ").splitlines() or [" "]
    longest = max(len(ln) for ln in lines)
    width = min(14_000.0, max(72.0, size * (0.75 * longest + 2)))
    height = min(14_000.0, size * (1.35 * len(lines) + 1))
    for _ in range(4):
        doc = pymupdf.open()
        page = doc.new_page(width=width, height=height)
        if text_box(page, page.rect, text, font, size, color, align=align, bold=bold, italic=italic,
                    opacity=opacity, shrink=False, text_layer=False):
            break
        doc.close()
        width, height = min(14_000.0, width * 2), min(14_000.0, height * 1.5)
    else:
        raise ValueError("the text is too long to place")
    box = pymupdf.Rect()
    for kind, r in page.get_bboxlog():
        if kind == "ignore-text":  # invisible text (the search layer of complex scripts) has no ink
            continue
        rr = pymupdf.Rect(r)
        if not rr.is_empty and not rr.is_infinite:
            box |= rr
    if box.is_empty:
        box = pymupdf.Rect(0, 0, size, size)
    box = (box + (-1, -1, 1, 1)) & page.rect
    if needs_text_layer(text) and text.strip():
        try:  # the search layer goes over the ink, so cropping to it keeps it
            _fix_text_layer(page, set(), box, text, 0, True)
        except Exception:
            pass
    page.set_cropbox(box)
    return doc, pymupdf.Rect(0, 0, box.width, box.height)
