"""Shared pytest fixtures and image factories (synthetic images only — nothing binary in git).

Factories are plain functions, imported where needed (``from tests.conftest import make_image``).
"""
from __future__ import annotations

import io
import struct
from collections.abc import Iterator

import numpy as np
import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

QUADRANT_COLORS = {"tl": (255, 0, 0), "tr": (0, 255, 0), "bl": (0, 0, 255), "br": (255, 255, 255)}


def _gradient(w: int, h: int) -> np.ndarray:
    x = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :]
    y = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
    r = np.broadcast_to(x * 255, (h, w))
    g = np.broadcast_to(y * 255, (h, w))
    b = (1.0 - (x + y) / 2) * 200 + 30
    return np.stack([r, g, np.broadcast_to(b, (h, w))], axis=-1)


def make_image(size: tuple[int, int] = (64, 48), mode: str = "RGB", kind: str = "gradient", *,
               color: tuple[int, ...] | str = (200, 60, 40), seed: int = 0) -> Image.Image:
    """Synthetic image. kind: gradient | noise | solid | photo (gradient + noise) | quadrants.

    ``quadrants`` paints TL red, TR green, BL blue, BR white (handy for orientation checks).
    RGBA/LA images get a horizontal alpha ramp (0 at the left edge) unless kind == "solid".
    """
    w, h = size
    rng = np.random.default_rng(seed)
    if kind == "gradient":
        arr = _gradient(w, h)
    elif kind == "noise":
        arr = rng.integers(0, 256, (h, w, 3)).astype(np.float32)
    elif kind == "photo":
        arr = _gradient(w, h) + rng.normal(0.0, 20.0, (h, w, 3))
    elif kind == "solid":
        return Image.new(mode, size, color if mode not in ("L", "1", "I;16") or not isinstance(color, tuple)
                         else color[0])
    elif kind == "quadrants":
        arr = np.zeros((h, w, 3), dtype=np.float32)
        arr[: h // 2, : w // 2] = QUADRANT_COLORS["tl"]
        arr[: h // 2, w // 2:] = QUADRANT_COLORS["tr"]
        arr[h // 2:, : w // 2] = QUADRANT_COLORS["bl"]
        arr[h // 2:, w // 2:] = QUADRANT_COLORS["br"]
    else:
        raise ValueError(f"unknown kind {kind!r}")
    rgb = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    if mode == "RGB":
        return rgb
    if mode in ("RGBA", "LA"):
        base = rgb if mode == "RGBA" else rgb.convert("L")
        ramp = np.broadcast_to(np.linspace(0, 255, w, dtype=np.float32)[None, :], (h, w))
        base.putalpha(Image.fromarray(ramp.astype(np.uint8)))
        return base
    if mode == "I;16":
        gray = np.asarray(rgb.convert("L"), dtype=np.uint16) * 257
        return Image.fromarray(gray)
    if mode == "P":
        return rgb.quantize(64)
    return rgb.convert(mode)


def _exif(orientation: int | None = None, gps: bool = False, camera: bool = False,
          dpi: int | None = None) -> Image.Exif:
    exif = Image.Exif()
    if orientation is not None:
        exif[0x0112] = orientation
    if camera:
        exif[0x010F] = "Apple"
        exif[0x0110] = "iPhone 15 Pro"
        exif[0x0132] = "2026:09:01 10:22:01"
        sub = exif.get_ifd(0x8769)
        sub[0x829A] = IFDRational(1, 120)
        sub[0x829D] = IFDRational(18, 10)
        sub[0x8827] = 80
        sub[0x9003] = "2026:09:01 10:22:01"
        sub[0x920A] = IFDRational(68, 10)
        sub[0xA434] = "iPhone 15 Pro back triple camera 6.86mm f/1.78"
    if gps:
        g = exif.get_ifd(0x8825)
        g[1] = "S"
        g[2] = (IFDRational(12, 1), IFDRational(58, 1), IFDRational(1200, 100))
        g[3] = "W"
        g[4] = (IFDRational(77, 1), IFDRational(35, 1), IFDRational(24, 1))
    if dpi is not None:
        exif[0x011A] = float(dpi)
        exif[0x011B] = float(dpi)
        exif[0x0128] = 2
    return exif


def make_exif_bytes(orientation: int | None = None, gps: bool = False, camera: bool = False,
                    dpi: int | None = None) -> bytes:
    """EXIF block (with ``Exif\\0\\0`` header); ``gps`` is S 12°58'12" W 77°35'24"."""
    return _exif(orientation, gps, camera, dpi).tobytes()


def make_jpeg_bytes(size: tuple[int, int] = (300, 200), orientation: int | None = None, gps: bool = False,
                    dpi: int | None = None, *, kind: str = "quadrants", camera: bool = False,
                    quality: int = 92, icc_profile: bytes | None = None, mode: str = "RGB") -> bytes:
    """JPEG of ``make_image(size, kind=kind)`` with optional EXIF orientation/GPS/camera and JFIF dpi."""
    img = make_image(size, mode, kind)
    params: dict = {"quality": quality}
    exif = _exif(orientation, gps, camera)
    if len(exif):
        params["exif"] = exif.tobytes()
    if dpi is not None:
        params["dpi"] = (dpi, dpi)
    if icc_profile is not None:
        params["icc_profile"] = icc_profile
    buf = io.BytesIO()
    img.save(buf, "JPEG", **params)
    return buf.getvalue()


def make_heic_bytes(size: tuple[int, int] = (300, 200), orientation: int | None = None, *,
                    kind: str = "quadrants", quality: int = 90, gps: bool = False, camera: bool = False,
                    mode: str = "RGB") -> bytes:
    """HEIC written by pillow-heif; EXIF orientation is stored as irot/imir like an iPhone file."""
    from app.imaging import io as imgio

    imgio.register_plugins()
    img = make_image(size, mode, kind)
    params: dict = {"quality": quality}
    exif = _exif(orientation, gps, camera)
    if len(exif):
        params["exif"] = exif.tobytes()
    buf = io.BytesIO()
    img.save(buf, "HEIF", **params)
    return buf.getvalue()


def encode(img: Image.Image, fmt: str, **params) -> bytes:
    """Encode with Pillow directly (for building test inputs)."""
    buf = io.BytesIO()
    img.save(buf, fmt, **params)
    return buf.getvalue()


# ICC v2 matrix/TRC profile builder (independent of app code so it can validate app/imaging/io.py).
_D50 = (0.9642, 1.0, 0.8249)
_PRIMARIES = {
    # D50-adapted colorants.
    "p3": ((0.515121, 0.241196, -0.001053), (0.291977, 0.692245, 0.041885), (0.157104, 0.066574, 0.784073)),
    "srgb": ((0.436066, 0.222488, 0.013916), (0.385147, 0.716873, 0.097076), (0.143066, 0.060608, 0.714096)),
}


def make_icc_profile(description: str = "Display P3 (test)", primaries: str = "p3", gamma: float = 2.2) -> bytes:
    """Minimal RGB display profile (ICC v2): desc, cprt, wtpt, r/g/bXYZ and a gamma TRC."""
    def s15(v: float) -> bytes:
        return struct.pack(">i", int(round(v * 65536)))

    def xyz(v: tuple[float, float, float]) -> bytes:
        return b"XYZ \0\0\0\0" + s15(v[0]) + s15(v[1]) + s15(v[2])

    text = description.encode("ascii") + b"\0"
    desc = b"desc\0\0\0\0" + struct.pack(">I", len(text)) + text + struct.pack(">IIHB", 0, 0, 0, 0) + b"\0" * 67
    curve = b"curv\0\0\0\0" + struct.pack(">IH", 1, int(round(gamma * 256))) + b"\0\0"
    red, green, blue = _PRIMARIES[primaries]
    tags = [(b"desc", desc), (b"cprt", b"text\0\0\0\0test\0"), (b"wtpt", xyz(_D50)), (b"rXYZ", xyz(red)),
            (b"gXYZ", xyz(green)), (b"bXYZ", xyz(blue)), (b"rTRC", curve), (b"gTRC", curve), (b"bTRC", curve)]
    table_len = 4 + 12 * len(tags)
    data, entries = b"", []
    for sig, payload in tags:
        data += b"\0" * (-len(data) % 4)
        entries.append(struct.pack(">4sII", sig, 128 + table_len + len(data), len(payload)))
        data += payload
    data += b"\0" * (-len(data) % 4)
    total = 128 + table_len + len(data)
    header = (struct.pack(">I4sI4s4s4s", total, b"\0\0\0\0", 0x02100000, b"mntr", b"RGB ", b"XYZ ")
              + struct.pack(">6H", 2024, 1, 1, 0, 0, 0) + b"acsp" + b"\0" * 28
              + s15(_D50[0]) + s15(_D50[1]) + s15(_D50[2]) + b"\0" * 48)
    return header + struct.pack(">I", len(tags)) + b"".join(entries) + data


def make_gray_icc_profile(gamma: float = 1.8, description: str = "Gray Gamma 1.8 (test)") -> bytes:
    """Minimal GRAY display profile (ICC v2): desc, cprt, wtpt and a gamma kTRC."""
    def s15(v: float) -> bytes:
        return struct.pack(">i", int(round(v * 65536)))

    text = description.encode("ascii") + b"\0"
    desc = b"desc\0\0\0\0" + struct.pack(">I", len(text)) + text + struct.pack(">IIHB", 0, 0, 0, 0) + b"\0" * 67
    tags = [(b"desc", desc), (b"cprt", b"text\0\0\0\0test\0"), (b"wtpt", b"XYZ \0\0\0\0" + b"".join(s15(v) for v in _D50)),
            (b"kTRC", b"curv\0\0\0\0" + struct.pack(">IH", 1, int(round(gamma * 256))) + b"\0\0")]
    table_len = 4 + 12 * len(tags)
    data, entries = b"", []
    for sig, payload in tags:
        data += b"\0" * (-len(data) % 4)
        entries.append(struct.pack(">4sII", sig, 128 + table_len + len(data), len(payload)))
        data += payload
    data += b"\0" * (-len(data) % 4)
    header = (struct.pack(">I4sI4s4s4s", 128 + table_len + len(data), b"\0\0\0\0", 0x02100000, b"mntr", b"GRAY",
                          b"XYZ ") + struct.pack(">6H", 2024, 1, 1, 0, 0, 0) + b"acsp" + b"\0" * 28
              + b"".join(s15(v) for v in _D50) + b"\0" * 48)
    return header + struct.pack(">I", len(tags)) + b"".join(entries) + data


def quadrant_colors(img: Image.Image, inset: int = 4) -> dict[str, tuple[int, ...]]:
    """Colour sampled near each corner (RGB)."""
    rgb = img.convert("RGB")
    w, h = rgb.size
    return {
        "tl": rgb.getpixel((inset, inset)),
        "tr": rgb.getpixel((w - 1 - inset, inset)),
        "bl": rgb.getpixel((inset, h - 1 - inset)),
        "br": rgb.getpixel((w - 1 - inset, h - 1 - inset)),
    }


def close_color(a: tuple[int, ...], b: tuple[int, ...], tol: int = 12) -> bool:
    return all(abs(int(x) - int(y)) <= tol for x, y in zip(a, b))


# ------------------------------------------------------------------ input factories


def make_dng_bytes(size: tuple[int, int] = (64, 48), *, orientation: int = 1, make: bytes = b"heypdf",
                   levels: tuple[int, int, int] = (900, 600, 300)) -> bytes:
    """Minimal uncompressed 16-bit RGGB DNG (LibRaw decodes it): R/G/B sensor levels of 4095."""
    w, h = size
    r, g, b = levels
    ramp = np.linspace(0.15, 1.0, w, dtype=np.float32)[None, :]   # darker left → brighter right
    cfa = np.zeros((h, w), np.float32)
    cfa[0::2, 0::2], cfa[0::2, 1::2], cfa[1::2, 0::2], cfa[1::2, 1::2] = r, g, g, b
    cfa = (cfa * ramp).astype(np.uint16)
    data = cfa.astype("<u2").tobytes()
    SHORT, LONG, ASCII, BYTE, SRATIONAL = 3, 4, 2, 1, 10
    color_matrix = b"".join(struct.pack("<ii", v * 10000, 10000) for v in (1, 0, 0, 0, 1, 0, 0, 0, 1))
    entries = sorted([
        (254, LONG, 1, struct.pack("<I", 0)), (256, LONG, 1, struct.pack("<I", w)),
        (257, LONG, 1, struct.pack("<I", h)), (258, SHORT, 1, struct.pack("<HH", 16, 0)),
        (259, SHORT, 1, struct.pack("<HH", 1, 0)), (262, SHORT, 1, struct.pack("<HH", 32803, 0)),
        (271, ASCII, len(make) + 1, make + b"\0"), (272, ASCII, 5, b"test\0"), (273, LONG, 1, None),
        (274, SHORT, 1, struct.pack("<HH", orientation, 0)), (277, SHORT, 1, struct.pack("<HH", 1, 0)),
        (278, LONG, 1, struct.pack("<I", h)), (279, LONG, 1, struct.pack("<I", len(data))),
        (284, SHORT, 1, struct.pack("<HH", 1, 0)), (33421, SHORT, 2, struct.pack("<HH", 2, 2)),
        (33422, BYTE, 4, bytes([0, 1, 1, 2])), (50706, BYTE, 4, bytes([1, 4, 0, 0])),
        (50707, BYTE, 4, bytes([1, 1, 0, 0])), (50708, ASCII, 12, b"heypdf test\0"),
        (50717, LONG, 1, struct.pack("<I", 4095)), (50721, SRATIONAL, 9, color_matrix),
        (50778, SHORT, 1, struct.pack("<HH", 21, 0)),
    ], key=lambda e: e[0])
    first_blob = 8 + 2 + 12 * len(entries) + 4
    blobs, fields = b"", []
    for tag, typ, count, value in entries:
        if value is None:
            fields.append((tag, typ, count, None))
        elif len(value) <= 4:
            fields.append((tag, typ, count, value.ljust(4, b"\0")))
        else:
            fields.append((tag, typ, count, struct.pack("<I", first_blob + len(blobs))))
            blobs += value + (b"\0" if len(value) % 2 else b"")
    strip = first_blob + len(blobs)
    ifd = struct.pack("<H", len(fields)) + b"".join(
        struct.pack("<HHI", tag, typ, count) + (value if value is not None else struct.pack("<I", strip))
        for tag, typ, count, value in fields) + struct.pack("<I", 0)
    return b"II*\0" + struct.pack("<I", 8) + ifd + blobs + data


def make_psd_bytes(size: tuple[int, int] = (40, 30), *, depth: int = 8, psb: bool = False,
                   color: tuple[float, float, float] = (1.0, 0.0, 0.0), layers: int = 0) -> bytes:
    """PSD/PSB written by psd-tools: a solid ``color`` (0–1 floats) document, optionally with layers."""
    from psd_tools import PSDImage

    psd = PSDImage.new("RGB", size, color=(1.0, 1.0, 1.0) if layers else color, depth=depth)
    if layers:
        psd.create_pixel_layer(Image.new("RGB", size, tuple(int(c * 255) for c in color)), name="background")
        for i in range(1, layers):
            psd.create_pixel_layer(Image.new("RGB", (4, 4), (0, 255 - i, 0)), name=f"layer {i}")
    if psb:
        psd._record.header.version = 2  # noqa: SLF001 — psd-tools writes the large-document variant
    buf = io.BytesIO()
    psd.save(buf)
    return buf.getvalue()


def make_svg(body: str = '<rect x="10" y="10" width="80" height="60" fill="#ff0000"/>', *, width: str | None = "100",
             height: str | None = "80", view_box: str | None = "0 0 100 80", extra: str = "") -> bytes:
    attrs = "".join(f' {k}="{v}"' for k, v in (("width", width), ("height", height), ("viewBox", view_box)) if v)
    return f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"{attrs}{extra}>{body}</svg>'.encode()


# ------------------------------------------------------------------ fixtures


@pytest.fixture(scope="session")
def p3_profile() -> bytes:
    return make_icc_profile()


@pytest.fixture
def pixel_limit(monkeypatch) -> Iterator:
    """Call ``pixel_limit(n)`` to set HEYP_MAX_PIXELS for one test; restored afterwards."""
    from app.imaging import io as imgio

    def set_limit(n: int) -> None:
        monkeypatch.setenv("HEYP_MAX_PIXELS", str(n))
        imgio.register_plugins()

    yield set_limit
    monkeypatch.undo()
    imgio.register_plugins()
