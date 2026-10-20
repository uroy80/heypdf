"""Bundled fonts for SVG text (``app/assets/fonts``, OFL / Bitstream Vera licences — see its README.md).

resvg rasterises an uploaded SVG with these files only (never system fonts). Text in Arabic, Hebrew, the Indic
scripts, Thai and Chinese/Japanese/Korean is pointed at a matching Noto face (:func:`script_runs`), because resvg's
own per-glyph fallback can't re-shape those scripts and would leave boxes.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path

__all__ = ["FONT_DIR", "fallback_files", "script_runs"]

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"


@dataclass(frozen=True)
class Fallback:
    """A face preferred for the code point ``ranges`` of its script."""
    file: str
    bold_file: str | None
    ranges: tuple[tuple[int, int], ...]
    family: str         # the face's family name (SVG text is pointed at it)


_ARABIC = ((0x0600, 0x06FF), (0x0750, 0x077F), (0x0870, 0x08FF), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))
_HEBREW = ((0x0590, 0x05FF), (0xFB1D, 0xFB4F))
_CJK = ((0x1100, 0x11FF), (0x2E80, 0x2FDF), (0x3000, 0x33FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xA960, 0xA97F),
        (0xAC00, 0xD7FF), (0xF900, 0xFAFF), (0xFE30, 0xFE4F), (0xFF00, 0xFFEF))


def _noto(stem: str, ranges: tuple[tuple[int, int], ...], family: str) -> Fallback:
    return Fallback(f"{stem}-Regular.ttf", f"{stem}-Bold.ttf", ranges, family)


FALLBACKS: tuple[Fallback, ...] = (
    Fallback("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", (), "DejaVu Sans"),  # Latin, Greek, Cyrillic, symbols
    _noto("NotoNaskhArabic", _ARABIC, "Noto Naskh Arabic"),
    _noto("NotoSansHebrew", _HEBREW, "Noto Sans Hebrew"),
    _noto("NotoSansDevanagari", ((0x0900, 0x097F), (0xA8E0, 0xA8FF), (0x1CD0, 0x1CFF)), "Noto Sans Devanagari"),
    _noto("NotoSansBengali", ((0x0980, 0x09FF),), "Noto Sans Bengali"),
    _noto("NotoSansGurmukhi", ((0x0A00, 0x0A7F),), "Noto Sans Gurmukhi"),
    _noto("NotoSansGujarati", ((0x0A80, 0x0AFF),), "Noto Sans Gujarati"),
    _noto("NotoSansOriya", ((0x0B00, 0x0B7F),), "Noto Sans Oriya"),
    _noto("NotoSansTamil", ((0x0B80, 0x0BFF),), "Noto Sans Tamil"),
    _noto("NotoSansTelugu", ((0x0C00, 0x0C7F),), "Noto Sans Telugu"),
    _noto("NotoSansKannada", ((0x0C80, 0x0CFF),), "Noto Sans Kannada"),
    _noto("NotoSansMalayalam", ((0x0D00, 0x0D7F),), "Noto Sans Malayalam"),
    _noto("NotoSansThai", ((0x0E00, 0x0E7F),), "Noto Sans Thai"),
    Fallback("NotoSansCJKsc-Subset.otf", None, _CJK, "Noto Sans CJK SC"),
)
_JOINERS = frozenset("‌‍")   # ZWNJ / ZWJ steer Indic and Arabic shaping


def fallback_files() -> list[str]:
    """Every fallback face file that is installed (resvg ``font_files``)."""
    out: list[str] = []
    for fb in FALLBACKS:
        for name in (fb.file, fb.bold_file):
            if name and name not in out and (FONT_DIR / name).is_file():
                out.append(name)
    return out


def _attaches(ch: str) -> bool:
    """Spaces, punctuation, marks and joiners stay in the run before them (one shaping run per phrase)."""
    if ch.isspace() or ch in _JOINERS:
        return True
    cat = unicodedata.category(ch)
    return cat[0] in "MPZ" or cat in ("Nd", "Sk", "Cf")


def _script_family(ch: str) -> str | None:
    cp = ord(ch)
    for fb in FALLBACKS[1:]:
        if any(lo <= cp <= hi for lo, hi in fb.ranges):
            return fb.family
    return None


def script_runs(value: str) -> list[tuple[str, str | None]]:
    """Split ``value`` into (chunk, Noto family or None) runs for the scripts :data:`FALLBACKS` covers;
    spaces, punctuation and digits between two letters of one script stay in its run."""
    runs: list[tuple[str, str | None]] = []
    pending = ""
    for ch in value:
        family = _script_family(ch)
        if family is None and runs and runs[-1][1] is not None and _attaches(ch):
            if unicodedata.category(ch)[0] == "M" and not pending:
                family = runs[-1][1]   # a combining mark joins its base
            else:
                pending += ch          # a neutral inside a script run: decided by the next letter
                continue
        if pending:
            if family is not None and runs[-1][1] == family:
                runs[-1] = (runs[-1][0] + pending, family)
            else:
                runs.append((pending, None))
            pending = ""
        if runs and runs[-1][1] == family:
            runs[-1] = (runs[-1][0] + ch, family)
        else:
            runs.append((ch, family))
    if pending:
        runs.append((pending, None))
    merged: list[tuple[str, str | None]] = []
    for chunk, family in runs:
        if merged and merged[-1][1] == family:
            merged[-1] = (merged[-1][0] + chunk, family)
        else:
            merged.append((chunk, family))
    return merged
