"""Decode and normalise untrusted images (JPG → PDF, Scan to PDF, signature and stamp images, thumbnails).

``load`` returns the first frame EXIF-oriented exactly once in RGB/RGBA/L/LA, with its ICC profile; ``preview``
makes an sRGB display copy.

Inputs beyond Pillow's: PSD (Pillow's 8-bit composite) and PSB / 16- and 32-bit PSD
(psd-tools merged image — PSD "frames" are layers and are never iterated), SVG (sanitised, then
rasterised by resvg with a pixel cap and the bundled fonts only) and camera RAW (rawpy/LibRaw,
routed by magic bytes *before* Pillow, which would return a DNG's small TIFF preview).
"""
from __future__ import annotations

import functools
import gzip
import math
import os
import re
import struct
import threading
import warnings
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from io import BytesIO
from typing import Any, BinaryIO
from xml.etree import ElementTree as ET

import numpy as np
from PIL import ExifTags, Image, ImageCms, features


__all__ = [
    "FEATURES", "ImageError", "Loaded", "register_plugins", "max_pixels", "load", "preview", "sniff", "probe_size",
    "SVG_RASTER_AUX", "encode_svg_raster", "load_svg_raster",
]

FEATURES: dict[str, bool] = {"heic": False, "avif": False}
DEFAULT_MAX_PIXELS = 50_000_000  # matches app.config (HEYP_MAX_PIXELS default); phones produce 12–48 MP
RAW_FORMATS = ("dng", "cr2", "cr3", "nef", "arw", "raf", "orf", "rw2", "pef", "srw", "raw")

_PIL_TO_CANON = {
    "JPEG": "jpeg", "MPO": "jpeg", "PNG": "png", "WEBP": "webp", "AVIF": "avif", "HEIF": "heic",
    "TIFF": "tiff", "BMP": "bmp", "DIB": "bmp", "GIF": "gif", "ICO": "ico", "JPEG2000": "jp2", "PSD": "psd",
}
_ALIASES = {
    "jpg": "jpeg", "jpe": "jpeg", "jfif": "jpeg", "pjpeg": "jpeg", "mpo": "jpeg", "heif": "heic", "hif": "heic",
    "heics": "heic", "heifs": "heic", "tif": "tiff", "dib": "bmp", "x-ms-bmp": "bmp", "icon": "ico",
    "x-icon": "ico", "vnd.microsoft.icon": "ico", "jpeg2000": "jp2", "j2k": "jp2", "jpx": "jp2", "jpf": "jp2",
    "j2c": "jp2", "x-png": "png", "apng": "png", "vnd.adobe.photoshop": "psd", "x-photoshop": "psd",
    "svg+xml": "svg", "svgz": "svg", "x-adobe-dng": "dng", "x-canon-cr2": "cr2", "x-canon-cr3": "cr3",
    "x-nikon-nef": "nef", "x-sony-arw": "arw", "x-fuji-raf": "raf", "x-olympus-orf": "orf",
    "x-panasonic-rw2": "rw2", "x-pentax-pef": "pef", "x-samsung-srw": "srw", "x-dcraw": "raw",
}

_TAG_ORIENTATION = ExifTags.Base.Orientation  # 0x0112
_TAG_XRES, _TAG_YRES, _TAG_RES_UNIT = 0x011A, 0x011B, 0x0128
_TAG_EXIF_IFD, _TAG_GPS_IFD, _TAG_MAKERNOTE = 0x8769, 0x8825, 0x927C
# TIFF structural / bulky tags that must not be carried into another file's EXIF.
_TIFF_ONLY_TAGS = frozenset({
    254, 255, 256, 257, 258, 259, 262, 263, 266, 273, 277, 278, 279, 280, 281, 284, 292, 293, 317, 320,
    322, 323, 324, 325, 330, 338, 339, 340, 341, 347, 513, 514, 700, 33723, 34377, 34675, 37724,
})
_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180, 4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE, 6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_HEAD_BYTES = 65536

_lock = threading.Lock()
_registered = False
_max_pixels = DEFAULT_MAX_PIXELS
_open_formats: tuple[str, ...] = ()


class ImageError(Exception):
    """Decoding failure with the HTTP status the API should use (413 too large, 415 undecodable)."""

    def __init__(self, message: str, status: int = 415) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Loaded:
    image: Image.Image          # first frame, EXIF-oriented once, mode ∈ {RGB, RGBA, L, LA}
    format: str                 # canonical lowercase format
    exif: bytes | None          # orientation reset to 1
    icc_profile: bytes | None
    dpi: tuple[float, float] | None
    frames: int
    bit_depth: int
    orientation: int            # original EXIF orientation (1 if none)


# ------------------------------------------------------------------ setup
def _env_max_pixels() -> int:
    raw = os.environ.get("HEYP_MAX_PIXELS", "").strip().replace("_", "")
    if not raw:
        return DEFAULT_MAX_PIXELS
    try:
        value = int(float(raw))
    except ValueError:
        return DEFAULT_MAX_PIXELS
    return value if value > 0 else DEFAULT_MAX_PIXELS


def register_plugins() -> dict[str, bool]:
    """Register HEIF/AVIF support and the pixel limit; safe to call repeatedly (re-reads HEYP_MAX_PIXELS)."""
    global _registered, _max_pixels, _open_formats
    with _lock:
        if not _registered:
            try:
                import pillow_heif

                pillow_heif.register_heif_opener(thumbnails=False, depth_images=False, aux_images=False)
                FEATURES["heic"] = True
            except Exception:  # missing wheel / libheif → HEIC unsupported, everything else works
                FEATURES["heic"] = False
            FEATURES["avif"] = bool(features.check("avif"))
            Image.init()
            _open_formats = tuple(f for f in _PIL_TO_CANON if f in Image.OPEN)
            # We enforce the limit ourselves (413) right after opening; Pillow's warning adds nothing.
            warnings.filterwarnings("ignore", category=Image.DecompressionBombWarning)
            _registered = True
        _max_pixels = _env_max_pixels()
        Image.MAX_IMAGE_PIXELS = _max_pixels
    return dict(FEATURES)


def _ensure_registered() -> None:
    if not _registered:
        register_plugins()


def max_pixels() -> int:
    """Current decompression-bomb limit in pixels (HEYP_MAX_PIXELS)."""
    _ensure_registered()
    return _max_pixels


# ------------------------------------------------------------------ format names


# ------------------------------------------------------------------ helpers
def has_alpha(img: Image.Image) -> bool:
    """True when the image has an alpha channel that is not fully opaque."""
    if img.mode in ("RGBA", "LA", "PA", "RGBa", "La"):
        return img.getextrema()[-1][0] < 255
    return img.mode == "P" and "transparency" in img.info


def _round_dpi(value: float) -> float:
    nearest = round(value)
    return float(nearest) if abs(value - nearest) < 0.05 else round(value, 2)


def _valid_dpi(pair: Any) -> tuple[float, float] | None:
    try:
        x, y = (float(v) for v in pair)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(x) and math.isfinite(y)) or not (1 <= x <= 100_000 and 1 <= y <= 100_000):
        return None
    return _round_dpi(x), _round_dpi(y)


def _dpi_from(info: dict, exif: Image.Exif | None) -> tuple[float, float] | None:
    dpi = _valid_dpi(info.get("dpi")) if info.get("dpi") is not None else None
    if dpi or exif is None:
        return dpi
    try:
        xres, yres = exif.get(_TAG_XRES), exif.get(_TAG_YRES)
        unit = exif.get(_TAG_RES_UNIT, 2)
        if xres is None:
            return None
        yres = xres if yres is None else yres
        factor = {2: 1.0, 3: 2.54}.get(int(unit))
        if factor is None:
            return None
        return _valid_dpi((float(xres) * factor, float(yres) * factor))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _valid_orientation(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 1
    return number if 1 <= number <= 8 else 1


def _safe_getexif(im: Image.Image) -> Image.Exif:
    try:
        return im.getexif()
    except Exception:  # corrupt EXIF must never break decoding
        return Image.Exif()


def _parse_exif(data: bytes | Image.Exif | None) -> Image.Exif | None:
    if data is None:
        return None
    if isinstance(data, Image.Exif):
        data = data.tobytes()
    if not data:
        return None
    exif = Image.Exif()
    try:
        exif.load(bytes(data))
    except Exception:
        return None
    return exif if len(exif) else None


def _serialize_exif(exif: Image.Exif) -> bytes | None:
    try:
        return exif.tobytes() if len(exif) else None
    except Exception:
        return None


def _clean_exif(raw: bytes | None) -> bytes | None:
    """Re-serialise EXIF with orientation 1 and without TIFF structural tags; None if empty/unreadable."""
    exif = _parse_exif(raw)
    if exif is None:
        return None
    for tag in list(exif.keys()):
        if tag in _TIFF_ONLY_TAGS:
            del exif[tag]
    if _TAG_ORIENTATION in exif:
        exif[_TAG_ORIENTATION] = 1
    return _serialize_exif(exif)


def _bit_depth(im: Image.Image, fmt: str, head: bytes) -> int:
    try:
        if fmt == "heic" and im.info.get("bit_depth"):
            return int(im.info["bit_depth"])
        if fmt == "png" and head.startswith(_PNG_SIGNATURE) and len(head) > 24:
            return int(head[24])
        if fmt == "tiff":
            bps = getattr(im, "tag_v2", {}).get(258)
            if bps:
                return int(max(bps) if isinstance(bps, (tuple, list)) else bps)
    except (TypeError, ValueError):
        pass
    mode = im.mode
    if mode == "1":
        return 1
    if mode.startswith("I;16"):
        return 16
    if mode in ("I", "F"):
        return 32
    return 8


def _to_8bit_gray(img: Image.Image) -> Image.Image:
    """Scale 16/32-bit integer or float grayscale to 8-bit L."""
    arr = np.asarray(img)
    mode = img.mode
    if mode.startswith("I;16"):
        out = (arr.astype(np.uint32) + 128) // 257
    else:
        data = np.nan_to_num(arr.astype(np.float32), nan=0.0, posinf=0.0, neginf=0.0)
        lo, hi = float(data.min()), float(data.max())
        if mode == "F" and lo >= 0 and hi <= 1.0:
            out = data * 255.0
        elif lo >= 0 and hi <= 255:
            out = data
        elif lo >= 0 and hi <= 65535:
            out = data / 257.0
        else:
            out = (data - lo) * (255.0 / (hi - lo)) if hi > lo else np.zeros_like(data)
        out = np.rint(out)
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def _profile(icc: bytes | None) -> ImageCms.ImageCmsProfile | None:
    if not icc:
        return None
    try:
        return ImageCms.ImageCmsProfile(BytesIO(icc))
    except Exception:
        return None


def _profile_space(icc: bytes | None) -> str | None:
    prof = _profile(icc)
    if prof is None:
        return None
    try:
        return prof.profile.xcolor_space.strip().upper()
    except Exception:
        return None


def _is_srgb(prof: ImageCms.ImageCmsProfile) -> bool:
    try:
        return "srgb" in ImageCms.getProfileDescription(prof).lower()
    except Exception:
        return False


def _normalize_mode(img: Image.Image, icc: bytes | None = None) -> tuple[Image.Image, bytes | None]:
    """Convert any Pillow mode to RGB/RGBA/L/LA; CMYK goes through its ICC profile when present."""
    mode = img.mode
    if mode in ("RGB", "RGBA", "L", "LA"):
        return img, icc
    if mode == "LAB":  # Photoshop Lab documents
        try:
            transform = ImageCms.buildTransform(ImageCms.createProfile("LAB"), ImageCms.createProfile("sRGB"),
                                                "LAB", "RGB", renderingIntent=ImageCms.Intent.PERCEPTUAL)
            return ImageCms.applyTransform(img, transform), None
        except Exception:
            return img.getchannel(0).convert("L"), None  # lightness only: still a faithful grey rendering
    if mode in ("P", "PA"):
        transparent = mode == "PA" or "transparency" in img.info or (
            img.palette is not None and img.palette.mode == "RGBA")
        return img.convert("RGBA" if transparent else "RGB"), icc
    if mode == "1":
        return img.convert("L"), icc
    if mode == "CMYK":
        if _profile_space(icc) == "CMYK":
            try:
                rgb = ImageCms.profileToProfile(
                    img, _profile(icc), ImageCms.createProfile("sRGB"), outputMode="RGB",
                    renderingIntent=ImageCms.Intent.PERCEPTUAL, flags=ImageCms.Flags.BLACKPOINTCOMPENSATION)
                if rgb is not None:
                    return rgb, None
            except Exception:
                pass
        return img.convert("RGB"), None
    if mode.startswith("I;16") or mode in ("I", "F"):
        return _to_8bit_gray(img), icc
    if mode == "La":
        return img.convert("LA"), icc
    if mode in ("RGBa", "RGBX"):
        return img.convert("RGBA" if mode == "RGBa" else "RGB"), icc
    alpha = "A" in img.getbands() or "transparency" in img.info
    return img.convert("RGBA" if alpha else "RGB"), icc


def _detach(img: Image.Image) -> Image.Image:
    """New plain Image object sharing pixel memory, with no file handle and an empty ``info``.

    Encoders read ``info`` as a fallback (EXIF/ICC/XMP) and Pillow stores per-call settings on the
    image object (``encoderinfo``), so every encode gets its own object: cached images are shared
    between threads.
    """
    img.load()
    try:
        plain = img._new(img.im)  # noqa: SLF001 — avoids a full pixel copy of large images
    except Exception:
        plain = img.copy()
    plain.info = {}
    return plain


# ------------------------------------------------------------------ load
def _open_source(source: str | bytes | bytearray | memoryview | BinaryIO) -> tuple[BinaryIO, bool]:
    if isinstance(source, (bytes, bytearray, memoryview)):
        return BytesIO(bytes(source)), True
    if isinstance(source, (str, os.PathLike)):
        return open(source, "rb"), True  # noqa: SIM115 — closed by load()
    if hasattr(source, "read"):
        if hasattr(source, "seekable") and not source.seekable():
            return BytesIO(source.read()), True
        return source, False
    raise TypeError("load() expects a path, bytes or a binary file object")


def _too_many_pixels(width: int, height: int) -> ImageError:
    return ImageError(
        f"Image is {width}×{height} px ({width * height / 1e6:.0f} MP), above the "
        f"{_max_pixels / 1e6:.0f} MP limit", status=413)


def load(source: str | bytes | BinaryIO, *, name: str | None = None) -> Loaded:
    """Decode the first frame, apply EXIF orientation once and normalise the mode.

    SVG, PSD/PSB and camera RAW are recognised by their bytes (``name``'s extension is only a hint
    for TIFF-based RAW files). Raises ImageError(413) above HEYP_MAX_PIXELS and ImageError(415) for
    undecodable input.
    """
    _ensure_registered()
    fp, owned = _open_source(source)
    try:
        head = fp.read(_HEAD_BYTES)  # PNG bit depth, AVIF colour boxes, format sniffing
        fp.seek(0)  # Image.open rewinds to 0 as well
        kind = sniff(head)
        if kind == "svg":
            return _load_svg(fp, head)
        if kind in ("psd", "psb"):
            return _load_psd(fp, head)
        hint = name if name is not None else (source if isinstance(source, str) else getattr(fp, "name", None))
        hinted_raw = _ext(hint if isinstance(hint, str) else None) in RAW_FORMATS
        if kind in RAW_FORMATS or (hinted_raw and _is_tiff(head)):
            try:
                return _load_raw(fp, head, kind or "raw")
            except ImageError as exc:
                if exc.status == 413 or not _is_tiff(head):
                    raise
                fp.seek(0)  # a TIFF with DNG-like tags that LibRaw refuses: let Pillow try it
        try:
            im = Image.open(fp, formats=_open_formats)
        except Image.DecompressionBombError:
            raise ImageError(f"Image has too many pixels (limit {_max_pixels / 1e6:.0f} MP)", status=413) from None
        except Exception as exc:
            if _is_tiff(head) and kind is None:  # PEF/SRW/… whose raw IFD0 Pillow can't read
                fp.seek(0)
                return _load_raw(fp, head, _tiff_raw_kind(head) or "raw")
            raise ImageError("Unsupported or corrupt image") from exc
        with im:
            try:
                return _decode(im, head)
            except ImageError as exc:
                if exc.status == 413 or not _is_tiff(head) or kind is not None:
                    raise
            fp.seek(0)
            return _load_raw(fp, head, _tiff_raw_kind(head) or "raw")
    finally:
        if owned:
            fp.close()


def _decode(im: Image.Image, head: bytes, *, frames: int | None = None) -> Loaded:
    fmt = _PIL_TO_CANON.get(im.format or "")
    if fmt is None:
        raise ImageError(f"Unsupported image format: {im.format or 'unknown'}")
    width, height = im.size
    if width <= 0 or height <= 0:
        raise ImageError("Image has no pixels")
    if width * height > _max_pixels:
        raise _too_many_pixels(width, height)
    if frames is None:
        try:
            frames = max(1, int(getattr(im, "n_frames", 1) or 1))
        except Exception:
            frames = 1
    # pillow-heif applies irot/imir itself and resets EXIF orientation to 1 (keeping the original in
    # info). Pillow's TIFF reader transposes on load and drops the tag. Everything else is applied here.
    if fmt == "heic":
        orientation = _valid_orientation(im.info.get("original_orientation"))
    else:
        orientation = _valid_orientation(_safe_getexif(im).get(_TAG_ORIENTATION))
    bit_depth = _bit_depth(im, fmt, head)
    icc = im.info.get("icc_profile") or None
    if icc is not None and not isinstance(icc, bytes):
        icc = None
    if icc is None and fmt in ("heic", "avif") and im.mode in ("RGB", "RGBA"):
        icc = _nclx_profile(im, fmt, head)
    try:
        im.load()
    except Image.DecompressionBombError:
        raise _too_many_pixels(width, height) from None
    except MemoryError:
        raise ImageError("Image is too large to decode", status=413) from None
    except Exception as exc:
        raise ImageError("Could not decode image data (the file may be truncated or corrupt)") from exc

    exif_obj = _safe_getexif(im)
    pending = 1 if fmt == "heic" else _valid_orientation(exif_obj.get(_TAG_ORIENTATION))
    raw_exif = im.info.get("exif") if isinstance(im.info.get("exif"), bytes) else _serialize_exif(exif_obj)
    exif = _clean_exif(raw_exif)
    # Pillow reports 72 dpi for any JPEG whose EXIF lacks resolution tags; trust only real JFIF units.
    dpi_info = im.info if fmt != "jpeg" or im.info.get("jfif_unit") in (1, 2) else {}
    dpi = _dpi_from(dpi_info, exif_obj)

    img: Image.Image = im
    if pending != 1:
        img = img.transpose(_TRANSPOSE[pending])
    img, icc = _normalize_mode(img, icc)
    img = _detach(img)
    return Loaded(image=img, format=fmt, exif=exif, icc_profile=icc, dpi=dpi, frames=frames,
                  bit_depth=bit_depth, orientation=orientation)


# ------------------------------------------------------------------ inputs: sniffing
_TIFF_MAGIC = (b"II*\x00", b"MM\x00*")
_SVG_RE = re.compile(
    r"^\s*(?:<\?xml[^>]{0,400}\?>\s*)?(?:<!--.*?-->\s*)*(?:<!DOCTYPE[^\[>]{0,400}(?:\[.*?\])?\s*>\s*)?"
    r"(?:<!--.*?-->\s*)*<(?:[A-Za-z_][\w.-]*:)?svg[\s>/]", re.DOTALL)
_RAW_MAKES = (("NIKON", "nef"), ("SONY", "arw"), ("PENTAX", "pef"), ("RICOH", "pef"), ("SAMSUNG", "srw"),
              ("CANON", "cr2"), ("OLYMPUS", "orf"), ("OM DIGITAL", "orf"), ("PANASONIC", "rw2"), ("LEICA", "rw2"),
              ("FUJIFILM", "raf"))
SVG_MAX_BYTES = 20 * 1024 * 1024        # source (or decompressed .svgz) size
SVG_MAX_ELEMENTS = 200_000
SVG_MIN_LONG_SIDE = 1024                # small vector art is rasterised at this size (no quality loss)
SVG_MAX_PIXELS = 40_000_000
SVG_MAX_SIDE = 16_384
PSD_MAX_ALLOC_BYTES = 1536 * 1024 * 1024  # psd-tools peak allocation cap
RAW_TIMEOUT_S = 90


def _ext(name: str | None) -> str:
    if not name or "." not in name:
        return ""
    ext = name.rsplit(".", 1)[1].strip().lower()
    return _ALIASES.get(ext, ext)


def _is_tiff(head: bytes) -> bool:
    return head[:4] in _TIFF_MAGIC


def _tiff_ifd0(head: bytes) -> tuple[str, dict[int, tuple[int, int, bytes]]] | None:
    """(byte order, {tag: (type, count, 4-byte value field)}) of a TIFF's first IFD within ``head``."""
    if not _is_tiff(head) or len(head) < 8:
        return None
    bo = "<" if head[:2] == b"II" else ">"
    try:
        offset = struct.unpack(bo + "I", head[4:8])[0]
        count = struct.unpack(bo + "H", head[offset:offset + 2])[0]
        tags: dict[int, tuple[int, int, bytes]] = {}
        for i in range(min(count, 1024)):
            entry = head[offset + 2 + 12 * i: offset + 14 + 12 * i]
            if len(entry) < 12:
                break
            tag, typ, cnt = struct.unpack(bo + "HHI", entry[:8])
            tags[tag] = (typ, cnt, entry[8:12])
        return bo, tags
    except (struct.error, IndexError):
        return None


def _tiff_ascii(head: bytes, bo: str, entry: tuple[int, int, bytes]) -> str:
    typ, cnt, value = entry
    if typ != 2 or cnt <= 0:
        return ""
    raw = value[:cnt] if cnt <= 4 else head[struct.unpack(bo + "I", value)[0]:][:min(cnt, 256)]
    return raw.split(b"\x00", 1)[0].decode("latin-1", "replace").strip()


def _tiff_raw_kind(head: bytes) -> str | None:
    """RAW flavour of a TIFF-structured file (DNG, CR2, NEF, ARW, PEF, SRW…), None for plain TIFF."""
    if not _is_tiff(head):
        return None
    if head[8:10] == b"CR":
        return "cr2"
    parsed = _tiff_ifd0(head)
    if parsed is None:
        return None
    bo, tags = parsed
    if 50706 in tags:  # DNGVersion
        return "dng"
    make = _tiff_ascii(head, bo, tags[271]).upper() if 271 in tags else ""
    subfile = tags.get(254)
    reduced = subfile is not None and struct.unpack(bo + "I", subfile[2])[0] & 1 if subfile and subfile[0] == 4 else False
    # Camera RAWs keep a reduced-resolution preview in IFD0 and the sensor data in a SubIFD (330).
    if reduced and 330 in tags:
        for prefix, kind in _RAW_MAKES:
            if make.startswith(prefix):
                return kind
        return "raw"
    return None


def sniff(head: bytes) -> str | None:
    """Image formats Pillow must not decide: "svg", "psd", "psb" or a RAW kind; None → Pillow."""
    if head[:4] == b"8BPS" and len(head) >= 6:
        return "psb" if head[4:6] == b"\x00\x02" else "psd"
    if head.startswith(b"FUJIFILMCCD-RAW"):
        return "raf"
    if head[4:12] == b"ftypcrx ":
        return "cr3"
    if head[:4] in (b"IIRO", b"IIRS", b"MMOR"):
        return "orf"
    if head[:4] == b"IIU\x00":
        return "rw2"
    if _is_tiff(head):
        return _tiff_raw_kind(head)
    if head[:2] == b"\x1f\x8b":
        try:
            inner = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(head, 4096)
        except zlib.error:
            return None
        return "svg" if _looks_like_svg(inner) else None
    return "svg" if _looks_like_svg(head) else None


def _looks_like_svg(data: bytes) -> bool:
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    stripped = data.lstrip()
    if not stripped.startswith(b"<"):
        return False
    text = data[:8192].decode("utf-8", "replace")
    return _SVG_RE.match(text) is not None


def probe_size(source: str | bytes | BinaryIO) -> tuple[int, int] | None:
    """Pixel size from the header without decoding pixels (SVG: the size it would be rasterised at).

    None when unknown; callers then budget for the pixel limit.
    """
    _ensure_registered()
    fp, owned = _open_source(source)
    try:
        head = fp.read(_HEAD_BYTES)
        fp.seek(0)
        kind = sniff(head)
        if kind in ("psd", "psb"):
            header = _psd_header(head)
            return (header[2], header[1]) if header else None
        if kind == "svg":
            try:
                text = _read_svg_text(fp, head)
                root = ET.fromstring(text)
                w, h = svg_size(root)
                return _svg_target(w, h)
            except Exception:
                return None
        if kind in RAW_FORMATS:
            return None  # LibRaw must parse the file; the raw decoder checks the limit itself
        try:
            with Image.open(fp, formats=_open_formats) as im:
                return im.size
        except Exception:
            return None
    finally:
        if owned:
            fp.close()


def _isolated(fn: Callable[..., Any], *args: Any, timeout_s: float, mem_mb: int) -> Any:
    """Run a native decoder in PLATFORM's resource-limited child (app.core.sandbox) when it exists."""
    import multiprocessing

    if multiprocessing.current_process().daemon:
        # already inside a sandbox child (e.g. a heypdf job decoding an SVG/RAW): its limits apply, and a
        # daemonic process may not start children of its own
        return fn(*args)
    try:
        from app.core.sandbox import run_isolated
    except ImportError:
        return fn(*args)
    return run_isolated(fn, *args, timeout_s=timeout_s, mem_mb=mem_mb, cpu_s=int(timeout_s))


# ------------------------------------------------------------------ inputs: SVG
_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"
_HREF_ATTRS = ("href", f"{{{_XLINK_NS}}}href")
_SAFE_DATA_URI = re.compile(r"^\s*data:image/(?:png|jpeg|jpg|gif|webp);base64,", re.IGNORECASE)
_DROP_ELEMENTS = frozenset({"script", "foreignObject", "iframe", "embed", "object", "audio", "video", "handler",
                            "listener"})
_CSS_IMPORT = re.compile(r"@import[^;]*;?", re.IGNORECASE)
_CSS_FONT_FACE = re.compile(r"@font-face\s*\{[^}]*\}", re.IGNORECASE)
_CSS_URL = re.compile(r"url\(\s*(['\"]?)(?!#)[^)]*\1\s*\)", re.IGNORECASE)
_UNIT_PX = {"": 1.0, "px": 1.0, "pt": 96 / 72, "pc": 16.0, "mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96.0,
            "em": 16.0, "ex": 8.0}
_LENGTH_RE = re.compile(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(px|pt|pc|mm|cm|in|em|ex|%)?\s*$")
ET.register_namespace("", _SVG_NS)
ET.register_namespace("xlink", _XLINK_NS)


def _read_svg_text(fp: BinaryIO, head: bytes) -> bytes:
    """The SVG document bytes (gunzipped for .svgz), refusing anything above SVG_MAX_BYTES."""
    if head[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=fp) as gz:
                data = gz.read(SVG_MAX_BYTES + 1)
        except (OSError, EOFError, zlib.error) as exc:
            raise ImageError("Corrupt compressed SVG") from exc
    else:
        data = fp.read(SVG_MAX_BYTES + 1)
    if len(data) > SVG_MAX_BYTES:
        raise ImageError(f"SVG is larger than {SVG_MAX_BYTES // (1024 * 1024)} MB", status=413)
    return data


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


_ACTIVE_URL = re.compile(r"^\s*(?:javascript|vbscript|data:(?!image/(?:png|jpeg|jpg|gif|webp)[;,]))", re.IGNORECASE)
_CSS_ACTIVE_URL = re.compile(r"url\(\s*(['\"]?)\s*(?:javascript|vbscript):[^)]*\1\s*\)", re.IGNORECASE)

# resvg falls back to a SERIF face when none of the named families is installed, while browsers fall back
# to their sans default, so design-tool exports ("Arial", "Helvetica", "Roboto" without a generic) would
# turn serif. Every font-family list without a generic family gets one appended (img-09).
_GENERIC_FAMILIES = frozenset({"serif", "sans-serif", "monospace", "cursive", "fantasy"})
_SANS_GENERICS = frozenset({"system-ui", "ui-sans-serif", "ui-rounded", "-apple-system", "blinkmacsystemfont"})
_SERIF_HINTS = ("times", "georgia", "garamond", "cambria", "antiqua", "palatino", "baskerville", "didot", "bodoni",
                "caslon", "minion", "merriweather", "playfair", "lora", "fraunces", "charter", "constantia")
_MONO_HINTS = ("mono", "courier", "consolas", "menlo", "monaco", "lucida console", "code")
_CSS_FONT_DECL = re.compile(r"(?<![\w-])(font-family|font)(\s*:\s*)([^;}]*)", re.IGNORECASE)


def _with_generic_family(families: str) -> str:
    """``families`` (a CSS font-family list) with a generic family appended when it has none."""
    low = families.strip().lower()
    if not low or "var(" in low or low.split("!")[0].strip() in ("inherit", "initial", "unset", "revert"):
        return families
    names = [n.strip().strip("'\"").strip().lower() for n in low.split("!")[0].split(",")]
    # (the ``font`` shorthand puts style/size before the first family: "bold 12px/1.2 sans-serif")
    if any(n in _GENERIC_FAMILIES or (n.split() and n.split()[-1] in _GENERIC_FAMILIES) for n in names):
        return families
    if any(n in _SANS_GENERICS or (n.split() and n.split()[-1] in _SANS_GENERICS) for n in names):
        generic = "sans-serif"
    else:
        first = next((n for n in names if n), "")
        squashed = first.replace("-", " ")
        if any(h in squashed for h in _MONO_HINTS):
            generic = "monospace"
        elif (any(h in squashed for h in _SERIF_HINTS) or "serif" in squashed) and "sans" not in squashed:
            generic = "serif"
        else:
            generic = "sans-serif"
    body, bang, rest = families.partition("!")
    return f"{body.rstrip()}, {generic}{(' !' + rest) if bang else ''}"


def _fix_css_fonts(css: str) -> str:
    """Append a generic family to every ``font-family:``/``font:`` declaration in CSS text."""
    def repl(match: re.Match[str]) -> str:
        value = match.group(3)
        trail = value[len(value.rstrip()):]
        return f"{match.group(1)}{match.group(2)}{_with_generic_family(value.rstrip())}{trail}"

    return _CSS_FONT_DECL.sub(repl, css)


def sanitize_svg(data: bytes | str, *, keep_links: bool = False) -> str:
    """Parse untrusted SVG and return a self-contained copy safe to rasterise.

    Removes every external reference (resvg reads absolute file paths in ``href`` — verified), so
    only ``#fragment`` links and raster ``data:`` URIs survive; drops scripts, foreign objects,
    CSS ``@import``/``@font-face`` and non-fragment ``url()``s; expands internal entities through
    expat (which refuses entity-amplification bombs and never fetches external entities). Raises
    ImageError(415) for anything that isn't a well-formed SVG and 413 for oversized documents.

    ``keep_links=True`` (an optimised SVG handed back to its owner) only removes active content —
    scripts, event handlers, foreign objects and ``javascript:``/non-image ``data:`` URLs — and keeps
    ordinary links, which are never fetched here.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    if len(data) > SVG_MAX_BYTES:
        raise ImageError(f"SVG is larger than {SVG_MAX_BYTES // (1024 * 1024)} MB", status=413)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ImageError("Invalid SVG (not well-formed XML)") from exc
    except Exception as exc:  # expat amplification guard and friends
        raise ImageError("Invalid SVG") from exc
    if _local(root.tag) != "svg":
        raise ImageError("Not an SVG document")
    count = 0
    stack = [(root, 0)]
    while stack:
        elem, depth = stack.pop()
        count += 1
        if count > SVG_MAX_ELEMENTS:
            raise ImageError(f"SVG has too many elements (max {SVG_MAX_ELEMENTS})", status=413)
        if depth > 512:
            raise ImageError("SVG is nested too deeply")
        if isinstance(elem.tag, str) and not elem.tag.startswith("{"):
            elem.tag = f"{{{_SVG_NS}}}{elem.tag}"  # hand-written SVG without xmlns
        for name in list(elem.attrib):
            value = elem.attrib[name]
            local = _local(name).lower()
            if name in _HREF_ATTRS or local == "href":
                if keep_links:
                    if _ACTIVE_URL.match(value):
                        del elem.attrib[name]
                elif not (value.strip().startswith("#") or _SAFE_DATA_URI.match(value)):
                    del elem.attrib[name]
            elif local.startswith("on") or name == "{http://www.w3.org/XML/1998/namespace}base":
                del elem.attrib[name]
            elif "url(" in value.lower():
                elem.attrib[name] = _CSS_ACTIVE_URL.sub("none", value) if keep_links else _CSS_URL.sub("none", value)
            elif _ACTIVE_URL.match(value) and local in ("src", "action", "formaction", "values", "to", "from", "by"):
                del elem.attrib[name]
        if not keep_links:
            _normalize_svg_render_attrs(elem)
        for child in list(elem):
            if _local(child.tag) in _DROP_ELEMENTS or not isinstance(child.tag, str):
                elem.remove(child)
                continue
            if _local(child.tag) == "style" and child.text:
                if keep_links:
                    child.text = _CSS_ACTIVE_URL.sub("none", child.text)
                else:
                    child.text = _fix_css_fonts(
                        _CSS_URL.sub("none", _CSS_FONT_FACE.sub("", _CSS_IMPORT.sub("", child.text))))
            stack.append((child, depth + 1))
    if not keep_links:
        _point_script_text(root)
    return ET.tostring(root, encoding="unicode")


def _point_script_text(root: ET.Element) -> None:
    """Wrap Arabic, Hebrew, Indic, Thai and CJK runs of SVG text in ``<tspan font-family="Noto …">`` so resvg
    shapes each with its bundled face (img-02): its own per-glyph fallback can't re-shape Indic clusters and
    leaves boxes when a run starts in a Latin face."""
    from .fonts import script_runs

    def spans(value: str) -> tuple[str, list[ET.Element]]:
        lead, out = "", []
        for chunk, family in script_runs(value):
            if family is not None:
                span = ET.Element(f"{{{_SVG_NS}}}tspan", {"font-family": family})
                span.text = chunk
                out.append(span)
            elif out:
                out[-1].tail = (out[-1].tail or "") + chunk
            else:
                lead += chunk
        return lead, out

    def scripted(value: str | None) -> bool:
        return bool(value) and any(ord(ch) >= 0x0590 for ch in value)  # type: ignore[union-attr]

    for elem in list(root.iter()):
        if _local(elem.tag) not in ("text", "tspan", "textPath", "a"):
            continue
        children = list(elem)
        if not scripted(elem.text) and not any(scripted(c.tail) for c in children):
            continue
        rebuilt: list[ET.Element] = []
        if scripted(elem.text):
            elem.text, new = spans(elem.text or "")
            rebuilt += new
        for child in children:
            rebuilt.append(child)
            if scripted(child.tail):
                child.tail, new = spans(child.tail or "")
                rebuilt += new
        elem[:] = rebuilt


def _normalize_svg_render_attrs(elem: ET.Element) -> None:
    """Rasterising only: generic font fallbacks (img-09) and filter clamps (img-07) on one element."""
    for name in ("font-family", "style"):
        value = elem.attrib.get(name)
        if value:
            elem.attrib[name] = _with_generic_family(value) if name == "font-family" else _fix_css_fonts(value)
    _clamp_filter_primitive(elem)


# ------------------------------------------------------------------ SVG filter cost (img-07)
# resvg runs filters on the CPU at a per-pixel cost that a few bytes of markup can multiply. Measured in the dev image, in
# seconds per megapixel of filter region: every filtered element ≈ 0.08, plus per primitive ≈ 0.1, feTurbulence
# ≈ 0.25 + 0.09 × octaves, feMorphology ≈ 0.1 + 0.003 × (2r + 1)² (r in px: r = 30 costs 12 s/MP) and
# feConvolveMatrix ≈ 0.1 + 0.003 × order². Before rendering, the filters an SVG applies are costed against the
# raster size (each region from the element's own geometry when it is simple enough to read, else the whole
# canvas) and a document above SVG_FILTER_BUDGET_S is refused (413) instead of holding a job slot until
# SVG_RENDER_TIMEOUT_S. Nested transforms are not followed, so the estimate can be low; the timeout covers that.
SVG_RENDER_TIMEOUT_S = 15
SVG_FILTER_BUDGET_S = 10.0
SVG_MAX_OCTAVES = 4
SVG_MAX_FILTER_USES = 20_000      # filtered elements; beyond this the SVG is refused outright
_FE_FREE = frozenset({"feMergeNode", "feFuncR", "feFuncG", "feFuncB", "feFuncA", "feDistantLight", "fePointLight",
                      "feSpotLight"})
_FILTER_REF = re.compile(r"url\(\s*['\"]?#([^'\")\s]+)['\"]?\s*\)")
_CSS_FILTER_DECL = re.compile(r"(?<![\w-])filter\s*:\s*([^;}]*)", re.IGNORECASE)
_NOT_RENDERED = frozenset({"defs", "symbol", "clipPath", "mask", "pattern", "marker", "linearGradient",
                           "radialGradient", "filter"})
_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_PATH_TOKEN = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_PATH_ARGS = {"m": 2, "l": 2, "h": 1, "v": 1, "c": 6, "s": 4, "q": 4, "t": 2, "a": 7}
_CONTAINERS = frozenset({"g", "a", "switch"})
_BBox = tuple[float, float, float, float]   # x0, y0, x1, y1 in user units


def _clamp_filter_primitive(elem: ET.Element) -> None:
    """feTurbulence octaves above SVG_MAX_OCTAVES add near-invisible detail at a linear cost: clamp them."""
    if _local(elem.tag) == "feTurbulence":
        raw = elem.attrib.get("numOctaves")
        if raw is not None:
            try:
                octaves = int(float(raw))
            except ValueError:
                return
            if octaves > SVG_MAX_OCTAVES:
                elem.attrib["numOctaves"] = str(SVG_MAX_OCTAVES)


def _numbers(value: str | None, default: float, count: int = 1) -> list[float]:
    parts = [abs(float(p)) for p in _NUMBER.findall(value or "")[:count]]
    parts = [p if math.isfinite(p) else 1e6 for p in parts] or [default]
    return (parts * count)[:count]


def _primitive_cost(prim: ET.Element, px_per_unit: float, bbox_side: float | None) -> float:
    """Seconds per megapixel of filter region for one primitive (see the rates above). ``bbox_side``: the
    element's size in px when primitiveUnits="objectBoundingBox" (radii are fractions of it)."""
    name = _local(prim.tag)
    if name == "feTurbulence":
        return 0.25 + 0.09 * min(SVG_MAX_OCTAVES, max(1.0, _numbers(prim.attrib.get("numOctaves"), 1)[0]))
    if name == "feMorphology":
        rx, ry = _numbers(prim.attrib.get("radius"), 0, 2)
        unit = bbox_side if bbox_side is not None else px_per_unit
        rx, ry = min(rx * unit, 1e5), min(ry * unit, 1e5)
        return 0.1 + 0.003 * (2 * rx + 1) * (2 * ry + 1)
    if name == "feConvolveMatrix":
        ox, oy = _numbers(prim.attrib.get("order"), 3, 2)
        return 0.1 + 0.003 * min(ox, 1e4) * min(oy, 1e4)
    return 0.1


def _frac(value: str | None, default: float) -> float:
    """A filter-region width/height in objectBoundingBox units (``1.2`` or ``120%``)."""
    if not value:
        return default
    value = value.strip()
    try:
        number = float(value[:-1]) / 100 if value.endswith("%") else float(value)
    except ValueError:
        return default
    return number if math.isfinite(number) and number > 0 else default


def _path_bbox(d: str) -> _BBox | None:
    """Bounding box of path data from its end and control points (arc ends padded by their diameters)."""
    tokens = _PATH_TOKEN.findall(d)
    if not tokens or len(tokens) > 200_000:
        return None
    x0 = y0 = math.inf
    x1 = y1 = -math.inf
    cx = cy = sx = sy = 0.0
    cmd = ""
    args: list[float] = []

    def add(x: float, y: float, pad_x: float = 0.0, pad_y: float = 0.0) -> None:
        nonlocal x0, y0, x1, y1
        x0, y0, x1, y1 = min(x0, x - pad_x), min(y0, y - pad_y), max(x1, x + pad_x), max(y1, y + pad_y)

    for token in tokens:
        if token.isalpha():
            cmd = token
            args = []
            if cmd in "Zz":
                cx, cy = sx, sy
            continue
        key = cmd.lower()
        if key not in _PATH_ARGS:
            return None
        args.append(float(token))
        if len(args) < _PATH_ARGS[key]:
            continue
        rel = cmd.islower()
        ox, oy = (cx, cy) if rel else (0.0, 0.0)
        if key == "h":
            cx = args[0] + ox
        elif key == "v":
            cy = args[0] + oy
        elif key == "a":
            cx, cy = args[5] + ox, args[6] + oy
            add(cx, cy, 2 * abs(args[0]), 2 * abs(args[1]))  # the arc stays within its circle
        else:
            for i in range(0, len(args) - 2, 2):
                add(args[i] + ox, args[i + 1] + oy)
            cx, cy = args[-2] + ox, args[-1] + oy
        add(cx, cy)
        if key == "m":
            sx, sy = cx, cy
            cmd = "l" if rel else "L"  # further pairs after a moveto are linetos
        args = []
    if not math.isfinite(x0):
        return None
    return x0, y0, x1, y1


def _union(boxes: list[_BBox | None]) -> _BBox | None:
    if not boxes or any(b is None for b in boxes):
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),  # type: ignore[index]
            max(b[2] for b in boxes), max(b[3] for b in boxes))  # type: ignore[index]


def svg_filter_cost(root: ET.Element, target: tuple[int, int]) -> float:
    """Estimated seconds resvg spends on filters to rasterise ``root`` at ``target`` px (see the rates above)."""
    tw, th = target
    canvas_px = float(tw) * float(th)
    w0, h0 = svg_size(root)
    vw, vh = (w0 or tw), (h0 or th)
    view = root.attrib.get("viewBox") or root.attrib.get("viewbox")
    if view:
        vb = [float(v) for v in _NUMBER.findall(view)]
        if len(vb) == 4 and vb[2] > 0 and vb[3] > 0 and all(math.isfinite(v) for v in vb):
            vw, vh = vb[2], vb[3]
    sx, sy = tw / vw, th / vh
    px_per_unit = max(sx, sy)
    ids: dict[str, ET.Element] = {}
    for elem in root.iter():
        ident = elem.attrib.get("id")
        if ident:
            ids.setdefault(ident, elem)
    # Instances of each element: rendered in place (outside <defs>) plus once per reference to it or an ancestor.
    refs: dict[str, int] = {}
    for elem in root.iter():
        for name, value in elem.attrib.items():
            if _local(name) == "href" and value.startswith("#"):
                refs[value[1:]] = refs.get(value[1:], 0) + 1
            elif "url(" in value and _local(name) != "filter":
                for ref in _FILTER_REF.findall(value):
                    refs[ref] = refs.get(ref, 0) + 1

    def length(elem: ET.Element, name: str, reference: float) -> float:
        raw = elem.attrib.get(name)
        if not raw:
            return 0.0
        raw = raw.strip()
        if raw.endswith("%"):
            try:
                return float(raw[:-1]) / 100 * reference
            except ValueError:
                return 0.0
        return _length(raw) or 0.0

    boxes: dict[int, _BBox | None] = {}

    def bbox(elem: ET.Element, depth: int = 0) -> _BBox | None:
        """User-space bounding box (transforms ignored), None when it can't be read cheaply."""
        key = id(elem)
        if key in boxes:
            return boxes[key]
        boxes[key] = None   # cycles (use → ancestor) stay unknown
        name = _local(elem.tag)
        box: _BBox | None = None
        try:
            if name in ("rect", "image", "svg", "foreignObject") or (name == "use" and "width" in elem.attrib):
                x, y = length(elem, "x", vw), length(elem, "y", vh)
                w = length(elem, "width", vw) if "width" in elem.attrib else (vw if name == "svg" else 0.0)
                h = length(elem, "height", vh) if "height" in elem.attrib else (vh if name == "svg" else 0.0)
                box = (x, y, x + w, y + h) if w > 0 and h > 0 else None
            elif name == "circle":
                r = length(elem, "r", math.hypot(vw, vh) / math.sqrt(2))
                x, y = length(elem, "cx", vw), length(elem, "cy", vh)
                box = (x - r, y - r, x + r, y + r)
            elif name == "ellipse":
                rx, ry = length(elem, "rx", vw), length(elem, "ry", vh)
                x, y = length(elem, "cx", vw), length(elem, "cy", vh)
                box = (x - rx, y - ry, x + rx, y + ry)
            elif name == "line":
                xa, xb, ya, yb = (length(elem, n, vw if n[0] == "x" else vh) for n in ("x1", "x2", "y1", "y2"))
                box = (min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb))
            elif name in ("polyline", "polygon"):
                pts = [float(v) for v in _NUMBER.findall(elem.attrib.get("points") or "")[:200_000]]
                if len(pts) >= 2:
                    box = (min(pts[0::2]), min(pts[1::2]), max(pts[0::2]), max(pts[1::2]))
            elif name == "path":
                box = _path_bbox(elem.attrib.get("d") or "")
            elif name == "use" and depth < 8:
                target_id = next((v[1:] for k, v in elem.attrib.items()
                                  if _local(k) == "href" and v.startswith("#")), "")
                inner = bbox(ids[target_id], depth + 1) if target_id in ids else None
                if inner is not None:
                    dx, dy = length(elem, "x", vw), length(elem, "y", vh)
                    box = (inner[0] + dx, inner[1] + dy, inner[2] + dx, inner[3] + dy)
            elif name in _CONTAINERS and depth < 64:
                kids = [c for c in elem if isinstance(c.tag, str) and _local(c.tag) not in _NOT_RENDERED
                        and _local(c.tag) not in ("title", "desc", "metadata", "style")]
                box = _union([bbox(c, depth + 1) for c in kids[:2000]]) if 0 < len(kids) <= 2000 else None
        except (ValueError, OverflowError, RecursionError):
            box = None
        if box is not None and not all(math.isfinite(v) for v in box):
            box = None
        boxes[key] = box
        return box

    filter_cache: dict[str, ET.Element | None] = {}

    def filter_node(ident: str) -> ET.Element | None:
        if ident not in filter_cache:
            node = ids.get(ident)
            filter_cache[ident] = node if node is not None and _local(node.tag) == "filter" else None
        return filter_cache[ident]

    def application_cost(node: ET.Element, elem: ET.Element) -> float:
        """Seconds for one element drawn through one filter."""
        box = None if node.attrib.get("filterUnits") == "userSpaceOnUse" else bbox(elem)
        if node.attrib.get("filterUnits") == "userSpaceOnUse":
            w, h = length(node, "width", vw) or vw * 1.2, length(node, "height", vh) or vh * 1.2
            region = w * h * sx * sy
        elif box is not None:
            bw, bh = box[2] - box[0], box[3] - box[1]
            region = bw * _frac(node.attrib.get("width"), 1.2) * bh * _frac(node.attrib.get("height"), 1.2) * sx * sy
        else:
            region = canvas_px
        region = min(canvas_px, max(0.0, region))
        side = None
        if node.attrib.get("primitiveUnits") == "objectBoundingBox":
            side = max(box[2] - box[0], box[3] - box[1]) * px_per_unit if box is not None else float(max(tw, th))
        prims = [p for p in node if isinstance(p.tag, str) and _local(p.tag).startswith("fe")
                 and _local(p.tag) not in _FE_FREE]
        rate = 0.08 + sum(_primitive_cost(p, px_per_unit, side) for p in prims)
        return rate * region / 1e6

    def applied(value: str | None) -> list[str]:
        if not value or "url(" not in value:
            return []
        return [r for r in _FILTER_REF.findall(value) if filter_node(r) is not None]

    # <style> rules that set a filter, by simple selector (.class, #id, tag, *); anything more complex is
    # matched as if it were "*" (an over-estimate). Parsed by splitting, not a regex, to stay linear; only
    # filters that exist are kept, once per selector.
    css: dict[str, set[str]] = {}
    for style in root.iter():
        if _local(style.tag) == "style" and style.text and "filter" in style.text.lower():
            for chunk in style.text.split("}"):
                selectors, brace, body = chunk.partition("{")
                if not brace or "filter" not in body.lower():
                    continue
                found = {r for decl in _CSS_FILTER_DECL.findall(body) for r in applied(decl)}
                if not found:
                    continue
                for sel in selectors.split(","):
                    sel = sel.strip()
                    css.setdefault(sel if re.fullmatch(r"[.#]?[\w-]+", sel) else "*", set()).update(found)

    def css_filters(elem: ET.Element) -> list[str]:
        out = list(css.get("*", ()))
        out += css.get(_local(elem.tag), ())
        ident = elem.attrib.get("id")
        if ident:
            out += css.get(f"#{ident}", ())
        for cls in (elem.attrib.get("class") or "").split()[:64]:
            out += css.get(f".{cls}", ())
        return out

    total = 0.0
    applications = 0
    budget = SVG_FILTER_BUDGET_S * 50  # stop counting once the answer is clear
    stack: list[tuple[ET.Element, int]] = [(root, 1)]
    while stack:
        elem, inherited = stack.pop()
        if not isinstance(elem.tag, str):
            continue
        name = _local(elem.tag)
        instances = (0 if name in _NOT_RENDERED else inherited) + refs.get(elem.attrib.get("id") or "", 0)
        if instances:
            wanted = applied(elem.attrib.get("filter"))
            style_value = elem.attrib.get("style")
            if style_value and "filter" in style_value:
                wanted += [r for decl in _CSS_FILTER_DECL.findall(style_value) for r in applied(decl)]
            if css:
                wanted += css_filters(elem)
            for ident in wanted:
                applications += 1
                total += instances * application_cost(filter_cache[ident], elem)  # type: ignore[arg-type]
                if total > budget or applications > SVG_MAX_FILTER_USES:
                    return max(total, budget)
        for child in elem:
            stack.append((child, instances))
    return total


def _length(value: str | None, reference: float | None = None) -> float | None:
    if not value:
        return None
    match = _LENGTH_RE.match(value)
    if not match:
        return None
    number, unit = float(match.group(1)), (match.group(2) or "").lower()
    if unit == "%":
        return number / 100 * reference if reference else None
    result = number * _UNIT_PX[unit]
    return result if math.isfinite(result) and result > 0 else None


def svg_size(root: ET.Element) -> tuple[float | None, float | None]:
    """Intrinsic (width, height) in CSS px from width/height (+ units) and the viewBox."""
    view = None
    raw_view = root.attrib.get("viewBox") or root.attrib.get("viewbox")
    if raw_view:
        try:
            parts = [float(v) for v in re.split(r"[\s,]+", raw_view.strip())]
            if len(parts) == 4 and parts[2] > 0 and parts[3] > 0 and all(math.isfinite(p) for p in parts):
                view = (parts[2], parts[3])
        except ValueError:
            view = None
    width = _length(root.attrib.get("width"), view[0] if view else None)
    height = _length(root.attrib.get("height"), view[1] if view else None)
    if view:
        if width is None and height is None:
            width, height = view
        elif width is None:
            width = height * view[0] / view[1]
        elif height is None:
            height = width * view[1] / view[0]
    return width, height


def _svg_target(width: float | None, height: float | None) -> tuple[int, int]:
    """Raster size for an SVG: intrinsic size, small art enlarged to SVG_MIN_LONG_SIDE, capped."""
    if not width or not height:
        width, height = width or height or 300.0, height or width or 150.0  # CSS default replaced size
    aspect = width / height
    if not 1e-4 <= aspect <= 1e4:
        raise ImageError("SVG has an extreme aspect ratio")
    long_side = max(width, height)
    scale = SVG_MIN_LONG_SIDE / long_side if long_side < SVG_MIN_LONG_SIDE else 1.0
    w, h = width * scale, height * scale
    limit = min(_max_pixels, SVG_MAX_PIXELS)
    shrink = min(1.0, math.sqrt(limit / (w * h)), SVG_MAX_SIDE / max(w, h))
    return max(1, int(round(w * shrink))), max(1, int(round(h * shrink)))


def _svg_fonts() -> tuple[list[str], dict[str, str]]:
    from .fonts import FONT_DIR, fallback_files

    static = ["DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSerif.ttf", "DejaVuSansMono.ttf", "Anton-Regular.ttf",
              "IBMPlexMono-Regular.ttf", "IBMPlexMono-Bold.ttf", "GreatVibes-Regular.ttf", "HomemadeApple-Regular.ttf"]
    # + the Noto faces text.py falls back to (Arabic, Hebrew, Indic, Thai, CJK): resvg picks a face per
    # character and shapes with rustybuzz, so SVG text in those scripts renders instead of boxes (img-02)
    static += [f for f in fallback_files() if f not in static]
    files = [str(FONT_DIR / f) for f in static if (FONT_DIR / f).is_file()]
    families = {"font_family": "DejaVu Sans", "sans_serif_family": "DejaVu Sans", "serif_family": "DejaVu Serif",
                "monospace_family": "DejaVu Sans Mono", "cursive_family": "Great Vibes", "fantasy_family": "Anton"}
    return files, families


def _resvg_png(svg_text: str, width: int, background: str | None, height: int | None = None) -> bytes:
    """resvg → PNG bytes (module-level so the sandbox child can import it)."""
    import tempfile

    import resvg_py

    files, families = _svg_fonts()
    size = {"width": int(width)} if height is None else {"width": int(width), "height": int(height)}
    with tempfile.TemporaryDirectory(prefix="heypdf-svg-") as empty:  # relative hrefs resolve into nothing
        return bytes(resvg_py.svg_to_bytes(svg_string=svg_text, background=background, skip_system_fonts=True,
                                           font_files=files, resources_dir=empty, **size, **families))


def render_svg(svg: bytes | str, *, width: int | None = None, height: int | None = None,
               background: str | None = None, sanitized: bool = False) -> Image.Image:
    """Rasterise SVG to RGBA. Without width/height the intrinsic size is used (see _svg_target);
    with one of them the other follows the aspect ratio. Untrusted input is sanitised first."""
    _ensure_registered()
    text = svg if sanitized and isinstance(svg, str) else sanitize_svg(svg)
    root = ET.fromstring(text.encode("utf-8"))
    w0, h0 = svg_size(root)
    tw, th = _svg_target(w0, h0)
    if width:
        th, tw = max(1, round(int(width) * th / tw)), int(width)
    elif height:
        tw, th = max(1, round(int(height) * tw / th)), int(height)
    if tw * th > _max_pixels or max(tw, th) > SVG_MAX_SIDE * 2:
        raise _too_many_pixels(tw, th)
    cost = svg_filter_cost(root, (tw, th))
    if cost > SVG_FILTER_BUDGET_S:
        raise ImageError(f"This SVG's filter effects are too heavy to render at {tw}×{th} px (about {cost:.0f} s "
                         "of work); simplify or remove the effects (blur, noise, morphology) and try again",
                         status=413)
    try:
        png = _isolated(_resvg_png, text, tw, background, th, timeout_s=SVG_RENDER_TIMEOUT_S, mem_mb=2048)
    except ImageError:
        raise
    except MemoryError:
        raise ImageError("SVG is too complex to render", status=413) from None
    except TimeoutError:
        raise ImageError("SVG took too long to render", status=413) from None
    except Exception as exc:
        raise ImageError("Could not render the SVG") from exc
    with Image.open(BytesIO(png), formats=["PNG"]) as im:
        im.load()
        out = im.convert("RGBA")
    if out.size != (tw, th):  # resvg rounds the derived side up; keep probe_size() exact
        out = out.resize((tw, th), Image.Resampling.LANCZOS)
    return out


SVG_RASTER_AUX = "svg-raster.png"   # store aux file: an upload's SVG is rendered once, then read back (img-07)


def encode_svg_raster(loaded: Loaded) -> bytes | None:
    """Fast PNG of a decoded SVG's raster for the upload's aux cache (None for anything but SVG)."""
    if loaded.format != "svg":
        return None
    buf = BytesIO()
    loaded.image.save(buf, "PNG", compress_level=1)
    return buf.getvalue()


def load_svg_raster(data: bytes) -> Loaded:
    """The ``Loaded`` that ``load`` gives for an SVG, from :func:`encode_svg_raster`'s PNG (no re-render)."""
    _ensure_registered()
    with Image.open(BytesIO(data), formats=["PNG"]) as im:
        im.load()
        img = im.copy() if im.mode in ("RGB", "RGBA") else im.convert("RGBA")
    if img.width * img.height > _max_pixels:
        raise _too_many_pixels(img.width, img.height)
    return Loaded(image=img, format="svg", exif=None, icc_profile=None, dpi=None, frames=1, bit_depth=8,
                  orientation=1)


def _load_svg(fp: BinaryIO, head: bytes) -> Loaded:
    data = _read_svg_text(fp, head)
    text = sanitize_svg(data)
    img = render_svg(text, sanitized=True)
    if img.width * img.height > _max_pixels:
        raise _too_many_pixels(img.width, img.height)
    if not has_alpha(img):
        img = img.convert("RGB")
    return Loaded(image=img, format="svg", exif=None, icc_profile=None, dpi=None, frames=1, bit_depth=8,
                  orientation=1)


# ------------------------------------------------------------------ inputs: PSD / PSB
def _psd_header(head: bytes) -> tuple[int, int, int, int, int, int] | None:
    """(version, height, width, depth, colour mode, channels) or None."""
    if len(head) < 26 or head[:4] != b"8BPS":
        return None
    version, channels, height, width, depth, mode = struct.unpack(">H6xHIIHH", head[4:26])
    if version not in (1, 2) or not 1 <= channels <= 56 or depth not in (1, 8, 16, 32):
        return None
    return version, height, width, depth, mode, channels


def _load_psd(fp: BinaryIO, head: bytes) -> Loaded:
    header = _psd_header(head)
    if header is None:
        raise ImageError("Unsupported or corrupt Photoshop file")
    version, height, width, depth, mode, _channels = header
    if width <= 0 or height <= 0:
        raise ImageError("Image has no pixels")
    if width * height > _max_pixels:
        raise _too_many_pixels(width, height)
    fmt = "psb" if version == 2 else "psd"
    layered_blank = False
    if version == 1 and depth == 8:
        # Pillow reads the merged composite; its n_frames counts LAYERS, so frames is forced to 1.
        try:
            with Image.open(fp, formats=["PSD"]) as im:
                layers = len(getattr(im, "layers", None) or [])
                loaded = _decode(im, head, frames=1)
            # A file saved without "Maximize compatibility" has a blank merged image: composite the layers.
            if not (layers and _uniform(loaded.image)):
                return loaded
            layered_blank = True
        except ImageError as exc:
            if exc.status == 413:
                raise
        except Exception:
            pass
        fp.seek(0)
    img = _psd_tools_image(fp, from_layers=layered_blank)
    if img.width * img.height > _max_pixels:
        raise _too_many_pixels(img.width, img.height)
    img, icc = _normalize_mode(img, None)
    return Loaded(image=_detach(img), format=fmt, exif=None, icc_profile=icc, dpi=None, frames=1,
                  bit_depth=depth, orientation=1)


def _uniform(img: Image.Image) -> bool:
    extrema = img.getextrema()
    if img.mode in ("L", "1", "P", "I", "F"):
        extrema = (extrema,)
    return all(lo == hi for lo, hi in extrema)


def _psd_tools_image(fp: BinaryIO, *, from_layers: bool = False) -> Image.Image:
    """Merged image of a PSB / 16-/32-bit PSD via psd-tools (ICC applied → sRGB); ``from_layers``
    composites the layers instead of the stored merged image."""
    try:
        from psd_tools import PSDImage
    except ImportError as exc:  # pragma: no cover — psd-tools ships in the image
        raise ImageError("Photoshop files with 16/32-bit colour or PSB are not supported on this server") from exc
    import logging

    logging.getLogger("psd_tools").setLevel(logging.ERROR)  # "Failed to apply ICC profile" on CMYK is noise
    try:
        psd = PSDImage.open(fp, max_alloc_bytes=PSD_MAX_ALLOC_BYTES)
        img = psd.topil() if psd.has_preview() and not from_layers else None
        if img is None or (len(psd) and _uniform(img)):
            img = psd.composite(ignore_preview=bool(len(psd)))
    except MemoryError:
        raise ImageError("Photoshop file is too large to decode", status=413) from None
    except Exception as exc:
        if "alloc" in str(exc).lower() or "limit" in str(exc).lower():
            raise ImageError("Photoshop file is too large to decode", status=413) from None
        raise ImageError("Unsupported or corrupt Photoshop file") from exc
    if img is None:
        raise ImageError("Photoshop file has no image data")
    img.load()
    return img


# ------------------------------------------------------------------ inputs: camera RAW
_LIBRAW_FLIP_TO_EXIF = {0: 1, 3: 3, 5: 8, 6: 6}


@dataclass
class _RawResult:
    rgb: np.ndarray | None   # None when the sensor is above the pixel limit
    width: int               # sensor size (before LibRaw's flip)
    height: int
    orientation: int         # EXIF-style orientation LibRaw applied (1 = none)


def _raw_decode(data: bytes | str, max_px: int) -> _RawResult:
    """LibRaw demosaic (camera white balance, 8-bit sRGB, the file's flip applied once). Module-level
    for the sandbox; refuses sensors above ``max_px`` before demosaicing."""
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    import rawpy

    raw = rawpy.imread(data) if isinstance(data, str) else rawpy.imread(BytesIO(data))
    with raw:
        sizes = raw.sizes
        width, height = int(sizes.width), int(sizes.height)
        if width <= 0 or height <= 0:
            raise ValueError("no pixels")
        flip = _LIBRAW_FLIP_TO_EXIF.get(int(sizes.flip), 1)
        if width * height > max_px:
            return _RawResult(None, width, height, flip)
        rgb = raw.postprocess(use_camera_wb=True, output_bps=8)
        return _RawResult(np.ascontiguousarray(rgb, dtype=np.uint8), width, height, flip)


RAW_EXIF_MAX_BYTES = 64 * 1024 * 1024


def _raw_exif(head: bytes, fp: BinaryIO) -> bytes | None:
    """EXIF of TIFF-structured RAWs (camera, date, GPS) read from the TIFF tags; no pixels decoded.
    DNG/private tags (≥ 0xC000) and the TIFF structure tags are dropped: they describe the sensor
    data, not the photo."""
    if not _is_tiff(head):
        return None
    exif: Image.Exif | None = None
    try:
        fp.seek(0)
        with Image.open(fp, formats=["TIFF"]) as im:
            exif = _safe_getexif(im)
    except Exception:  # IFD0 holds CFA data Pillow can't set up: parse the tags alone
        try:
            fp.seek(0)
            exif = Image.Exif()
            exif.load(fp.read(RAW_EXIF_MAX_BYTES))
        except Exception:
            return None
    try:
        for tag in [t for t in exif if t >= 0xC000 or t in _TIFF_ONLY_TAGS]:
            del exif[tag]
        return _clean_exif(_serialize_exif(exif)) if len(exif) else None
    except Exception:
        return None


def _load_raw(fp: BinaryIO, head: bytes, kind: str) -> Loaded:
    name = getattr(fp, "name", None)
    source: bytes | str = name if isinstance(name, str) and os.path.isfile(name) else fp.read()
    try:
        result: _RawResult = _isolated(_raw_decode, source, _max_pixels, timeout_s=RAW_TIMEOUT_S, mem_mb=3072)
    except MemoryError:
        raise ImageError("RAW file is too large to decode", status=413) from None
    except TimeoutError:
        raise ImageError("RAW file took too long to decode", status=413) from None
    except Exception as exc:
        raise ImageError("Unsupported or corrupt image (camera RAW not recognised)") from exc
    if result.rgb is None:
        raise _too_many_pixels(result.width, result.height)
    arr, orientation, bits = result.rgb, result.orientation, 16
    if arr.ndim != 3 or arr.shape[2] != 3 or arr.size == 0:
        raise ImageError("Unsupported or corrupt camera RAW")
    img = Image.fromarray(arr, "RGB")
    exif = _raw_exif(head, fp)
    if kind == "raw":
        kind = _tiff_raw_kind(head) or "raw"
    return Loaded(image=img, format=kind, exif=exif, icc_profile=None, dpi=None, frames=1, bit_depth=bits,
                  orientation=orientation)


# ------------------------------------------------------------------ colour
def to_srgb(img: Image.Image, icc_profile: bytes | None) -> Image.Image:
    """Convert pixels from ``icc_profile`` to sRGB (RGB/RGBA with an RGB profile, L/LA with a gray one).

    No-op without a profile, with an unusable or sRGB profile, or when profile and mode don't match.
    Gray images stay gray: the result is the sRGB rendering's (neutral) gray level.
    """
    if not icc_profile or img.mode not in ("RGB", "RGBA", "L", "LA"):
        return img
    prof = _profile(icc_profile)
    if prof is None or _is_srgb(prof):
        return img
    flags = ImageCms.Flags.BLACKPOINTCOMPENSATION
    try:
        space = prof.profile.xcolor_space.strip().upper()
        if img.mode in ("L", "LA"):
            if space != "GRAY":
                return img
            gray = img.getchannel("L") if img.mode == "LA" else img
            rgb = ImageCms.profileToProfile(gray, prof, ImageCms.createProfile("sRGB"), outputMode="RGB",
                                            renderingIntent=ImageCms.Intent.PERCEPTUAL, flags=flags)
            if rgb is None:
                return img
            out = rgb.convert("L")
            return Image.merge("LA", (out, img.getchannel("A"))) if img.mode == "LA" else out
        if space != "RGB":
            return img
        out = ImageCms.profileToProfile(
            img, prof, ImageCms.createProfile("sRGB"), outputMode=img.mode,
            renderingIntent=ImageCms.Intent.PERCEPTUAL, flags=flags)
    except Exception:  # broken or exotic profile → leave pixels untouched
        return img
    return out if out is not None else img


# sRGB IEC61966-2.1 as a compact ICC v2 profile (≈600 bytes; widest viewer compatibility).
_D50 = (0.9642, 1.0, 0.8249)


def _s15f16(value: float) -> bytes:
    return struct.pack(">i", int(round(value * 65536)))


def _srgb_eotf(v: float) -> float:
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def _build_rgb_icc(description: str, colorants: Any, eotf: Any = _srgb_eotf, samples: int = 64) -> bytes:
    """Matrix/TRC display profile: D50-adapted ``colorants`` (rows r, g, b) and a sampled ``eotf`` curve."""
    def xyz(x: float, y: float, z: float) -> bytes:
        return b"XYZ \0\0\0\0" + _s15f16(x) + _s15f16(y) + _s15f16(z)

    text = description.encode("ascii", "replace") + b"\0"
    desc = b"desc\0\0\0\0" + struct.pack(">I", len(text)) + text + struct.pack(">IIHB", 0, 0, 0, 0) + b"\0" * 67
    cprt = b"text\0\0\0\0No copyright, use freely\0"
    points = [min(65535, max(0, int(round(eotf(i / (samples - 1)) * 65535)))) for i in range(samples)]
    curve = b"curv\0\0\0\0" + struct.pack(">I", len(points)) + struct.pack(f">{len(points)}H", *points)
    red, green, blue = (tuple(float(c) for c in row) for row in colorants)
    tags = [(b"desc", desc), (b"cprt", cprt), (b"wtpt", xyz(*_D50)), (b"rXYZ", xyz(*red)),
            (b"gXYZ", xyz(*green)), (b"bXYZ", xyz(*blue)),
            (b"rTRC", curve), (b"gTRC", curve), (b"bTRC", curve)]
    table_len = 4 + 12 * len(tags)
    data, entries, offsets = b"", [], {}
    for sig, payload in tags:
        if payload not in offsets:
            data += b"\0" * (-len(data) % 4)
            offsets[payload] = 128 + table_len + len(data)
            data += payload
        entries.append(struct.pack(">4sII", sig, offsets[payload], len(payload)))
    data += b"\0" * (-len(data) % 4)
    size = 128 + table_len + len(data)
    header = (struct.pack(">I4sI4s4s4s", size, b"\0\0\0\0", 0x02100000, b"mntr", b"RGB ", b"XYZ ")
              + struct.pack(">6H", 2024, 1, 1, 0, 0, 0) + b"acsp" + b"\0" * 24 + struct.pack(">I", 0)
              + _s15f16(_D50[0]) + _s15f16(_D50[1]) + _s15f16(_D50[2]) + b"\0" * 48)
    return header + struct.pack(">I", len(tags)) + b"".join(entries) + data


# ------------------------------------------------------------------ nclx (CICP) colour → ICC
# HEIF/AVIF may describe colour with an nclx box (ITU-T H.273 codes) instead of an ICC profile.
# Primaries: code → (name, (rx, ry), (gx, gy), (bx, by), white xy).
_D65_XY = (0.3127, 0.3290)
_NCLX_PRIMARIES: dict[int, tuple[str, tuple[float, float], tuple[float, float], tuple[float, float],
                                 tuple[float, float]]] = {
    1: ("BT.709", (0.640, 0.330), (0.300, 0.600), (0.150, 0.060), _D65_XY),
    4: ("BT.470M", (0.670, 0.330), (0.210, 0.710), (0.140, 0.080), (0.310, 0.316)),
    5: ("BT.470BG", (0.640, 0.330), (0.290, 0.600), (0.150, 0.060), _D65_XY),
    6: ("BT.601", (0.630, 0.340), (0.310, 0.595), (0.155, 0.070), _D65_XY),
    7: ("SMPTE 240M", (0.630, 0.340), (0.310, 0.595), (0.155, 0.070), _D65_XY),
    8: ("Generic film", (0.681, 0.319), (0.243, 0.692), (0.145, 0.049), (0.310, 0.316)),
    9: ("BT.2020", (0.708, 0.292), (0.170, 0.797), (0.131, 0.046), _D65_XY),
    11: ("DCI-P3", (0.680, 0.320), (0.265, 0.690), (0.150, 0.060), (0.314, 0.351)),
    12: ("Display P3", (0.680, 0.320), (0.265, 0.690), (0.150, 0.060), _D65_XY),
    22: ("EBU 3213", (0.630, 0.340), (0.295, 0.605), (0.155, 0.077), _D65_XY),
}
# Transfer characteristics. Unspecified/reserved and the BT.709 family are rendered with the sRGB curve,
# as browsers do; PQ/HLG (16/18) need tone mapping and are left alone, like other exotic curves.
_NCLX_SRGB_LIKE = frozenset({0, 1, 2, 3, 6, 7, 13, 14, 15})
_NCLX_OTHER_CURVES: dict[int, tuple[str, Any]] = {
    4: ("gamma 2.2", lambda v: v ** 2.2),
    5: ("gamma 2.8", lambda v: v ** 2.8),
    8: ("linear", lambda v: v),
}
_BRADFORD = np.array([[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]])


def _colorants_from_xy(red: tuple[float, float], green: tuple[float, float], blue: tuple[float, float],
                       white: tuple[float, float]) -> np.ndarray:
    """D50-adapted (Bradford) RGB→XYZ colorants (rows r, g, b) for chromaticities ``red/green/blue/white``."""
    def xyz(xy: tuple[float, float]) -> np.ndarray:
        x, y = xy
        return np.array([x / y, 1.0, (1.0 - x - y) / y])

    prim = np.stack([xyz(red), xyz(green), xyz(blue)], axis=1)
    white_xyz = xyz(white)
    rgb_to_xyz = prim * np.linalg.solve(prim, white_xyz)
    adapt = np.linalg.inv(_BRADFORD) @ np.diag((_BRADFORD @ np.array(_D50)) / (_BRADFORD @ white_xyz)) @ _BRADFORD
    return (adapt @ rgb_to_xyz).T


@functools.lru_cache(maxsize=32)
def _nclx_icc(primaries: Any, transfer: Any) -> bytes | None:
    """ICC profile equivalent to an nclx colour description; None when it is sRGB or not representable."""
    if not isinstance(primaries, int) or not isinstance(transfer, int):
        return None
    if transfer in _NCLX_SRGB_LIKE:
        curve_name, eotf = "", _srgb_eotf
    elif transfer in _NCLX_OTHER_CURVES:
        curve_name, eotf = _NCLX_OTHER_CURVES[transfer]
    else:
        return None
    if primaries == 2 and curve_name:
        primaries = 1  # unspecified primaries with a non-sRGB curve: assume BT.709/sRGB primaries
    if primaries not in _NCLX_PRIMARIES or (primaries == 1 and not curve_name):
        return None
    name, red, green, blue, white = _NCLX_PRIMARIES[primaries]
    try:
        colorants = _colorants_from_xy(red, green, blue, white)
    except (np.linalg.LinAlgError, ZeroDivisionError):  # pragma: no cover — table values are sane
        return None
    label = f"{name}, {curve_name} (nclx)" if curve_name else f"{name} (nclx)"
    return _build_rgb_icc(label, colorants, eotf, samples=1024)


def _box_iter(data: bytes, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """ISOBMFF boxes in data[start:end] as (type, payload start, payload end); stops at truncation."""
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size, header = struct.unpack_from(">Q", data, pos + 8)[0], 16
        elif size == 0:
            size = end - pos
        if size < header:
            return
        end_pos = pos + size
        yield kind, pos + header, min(end_pos, end)
        if end_pos > end:
            return
        pos = end_pos


def _avif_nclx(head: bytes) -> tuple[int, int] | None:
    """(colour_primaries, transfer_characteristics) of the primary item's nclx 'colr' box in an AVIF.

    Pillow's AVIF plugin exposes only ICC profiles; the nclx box sits in meta/iprp/ipco and is tied to
    the primary item via ipma. Falls back to the first nclx property when there is no association.
    """
    try:
        meta = next(((s, e) for kind, s, e in _box_iter(head, 0, len(head)) if kind == b"meta"), None)
        if meta is None:
            return None
        primary, props, assoc = None, [], {}
        for kind, s, e in _box_iter(head, meta[0] + 4, meta[1]):  # meta is a FullBox
            if kind == b"pitm":
                primary = struct.unpack_from(">H" if head[s] == 0 else ">I", head, s + 4)[0]
            elif kind == b"iprp":
                for sub, s2, e2 in _box_iter(head, s, e):
                    if sub == b"ipco":
                        props = [(k, a, b) for k, a, b in _box_iter(head, s2, e2)]
                    elif sub == b"ipma":
                        assoc.update(_ipma(head, s2, e2))
        nclx = []
        for index, (kind, s, e) in enumerate(props, start=1):
            if kind == b"colr" and e - s >= 11 and head[s:s + 4] == b"nclx":
                nclx.append((index, struct.unpack_from(">HH", head, s + 4)))
        if not nclx:
            return None
        linked = set(assoc.get(primary, ()))
        for index, values in nclx:
            if index in linked:
                return values
        return nclx[0][1]
    except (struct.error, IndexError):
        return None


def _ipma(data: bytes, start: int, end: int) -> dict[int, list[int]]:
    """Item → 1-based property indices from an 'ipma' FullBox payload."""
    version, flags = data[start], int.from_bytes(data[start + 1:start + 4], "big")
    pos = start + 4
    count = struct.unpack_from(">I", data, pos)[0]
    pos += 4
    out: dict[int, list[int]] = {}
    for _ in range(min(count, 4096)):
        if version < 1:
            item = struct.unpack_from(">H", data, pos)[0]
            pos += 2
        else:
            item = struct.unpack_from(">I", data, pos)[0]
            pos += 4
        n = data[pos]
        pos += 1
        indices = []
        for _ in range(n):
            if flags & 1:
                indices.append(struct.unpack_from(">H", data, pos)[0] & 0x7FFF)
                pos += 2
            else:
                indices.append(data[pos] & 0x7F)
                pos += 1
        out[item] = indices
        if pos > end:
            break
    return out


def _nclx_profile(im: Image.Image, fmt: str, head: bytes) -> bytes | None:
    """ICC profile synthesised from a HEIF/AVIF nclx colour box (wide-gamut primaries only)."""
    if fmt == "heic":
        nclx = im.info.get("nclx_profile")
        if not isinstance(nclx, dict):
            return None
        codes = nclx.get("color_primaries"), nclx.get("transfer_characteristics")
    else:
        found = _avif_nclx(head)
        if found is None:
            return None
        codes = found
    if not all(type(c) is int for c in codes):
        return None
    return _nclx_icc(*codes)


# ------------------------------------------------------------------ save


# ------------------------------------------------------------------ preview
def preview(img: Image.Image, icc_profile: bytes | None = None, max_side: int = 1600) -> tuple[bytes, str]:
    """sRGB display copy ≤ ``max_side`` px: (JPEG q82 bytes, "image/jpeg") or PNG when alpha is present."""
    try:
        max_side = int(max_side)
    except (TypeError, ValueError):
        raise ValueError("max_side must be an integer") from None
    if max_side < 1:
        raise ValueError("max_side must be at least 1")
    img, icc_profile = _normalize_mode(img, icc_profile)
    width, height = img.size
    factor = min(1.0, max_side / max(width, height))
    if factor < 1.0:
        img = img.resize((max(1, round(width * factor)), max(1, round(height * factor))),
                         Image.Resampling.LANCZOS, reducing_gap=3.0)
    img = _detach(to_srgb(img, icc_profile))
    buf = BytesIO()
    if has_alpha(img):
        img.save(buf, "PNG", compress_level=3)
        return buf.getvalue(), "image/png"
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB" if img.mode == "RGBA" else "L")
    img.save(buf, "JPEG", quality=82)
    return buf.getvalue(), "image/jpeg"


