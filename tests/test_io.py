"""app.imaging.io: decoding, orientation, normalisation, colour management and display previews."""
from __future__ import annotations

import io as stdio
import numpy as np
import pytest
from PIL import Image, ImageCms, ImageOps

from app import config
from app.imaging import io
from tests.conftest import (QUADRANT_COLORS, close_color, encode, make_exif_bytes, make_heic_bytes, make_image,
                            make_jpeg_bytes, quadrant_colors)

io.register_plugins()

# Expected corner colours of the quadrant test image after each EXIF orientation is applied.
_ORIENTED_SIZE = {1: (300, 200), 2: (300, 200), 3: (300, 200), 4: (300, 200),
                  5: (200, 300), 6: (200, 300), 7: (200, 300), 8: (200, 300)}


def _assert_quadrants(img: Image.Image, expected: Image.Image, tol: int = 16) -> None:
    got, want = quadrant_colors(img), quadrant_colors(expected)
    for corner in got:
        assert close_color(got[corner], want[corner], tol), (corner, got, want)


def _exif_of_bytes(data: bytes | None) -> Image.Exif:
    exif = Image.Exif()
    if data:
        exif.load(data)
    return exif


def icc_description(icc: bytes | None) -> str | None:
    try:
        return ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(stdio.BytesIO(icc))).strip() if icc else None
    except Exception:
        return None


SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


# ------------------------------------------------------------------ setup & names
def test_register_plugins_is_idempotent_and_reports_features():
    first = io.register_plugins()
    second = io.register_plugins()
    assert first == second == {"heic": True, "avif": True}
    assert io.FEATURES == {"heic": True, "avif": True}
    assert Image.MAX_IMAGE_PIXELS == io.max_pixels() == io.DEFAULT_MAX_PIXELS == config.DEFAULT_MAX_PIXELS == 50_000_000


def test_pixel_limit_comes_from_env(pixel_limit):
    pixel_limit(12345)
    assert io.max_pixels() == 12345
    assert Image.MAX_IMAGE_PIXELS == 12345


# ------------------------------------------------------------------ orientation
@pytest.mark.parametrize("orientation", range(1, 9))
def test_jpeg_every_exif_orientation_applied_once(orientation):
    data = make_jpeg_bytes((300, 200), orientation=orientation)
    loaded = io.load(data)
    assert loaded.image.size == _ORIENTED_SIZE[orientation]
    assert loaded.orientation == orientation
    with Image.open(stdio.BytesIO(data)) as raw:
        reference = ImageOps.exif_transpose(raw)
    _assert_quadrants(loaded.image, reference)
    if loaded.exif is not None:
        assert _exif_of_bytes(loaded.exif).get(0x0112, 1) == 1


def test_jpeg_orientation_6_of_300x200_loads_as_200x300():
    loaded = io.load(make_jpeg_bytes((300, 200), orientation=6))
    assert loaded.image.size == (200, 300)
    # 90° clockwise: the original bottom-left (blue) is now top-left, top-left (red) is top-right.
    corners = quadrant_colors(loaded.image)
    assert close_color(corners["tl"], QUADRANT_COLORS["bl"])
    assert close_color(corners["tr"], QUADRANT_COLORS["tl"])
    assert close_color(corners["br"], QUADRANT_COLORS["tr"])
    assert close_color(corners["bl"], QUADRANT_COLORS["br"])
    exif = Image.Exif()
    exif.load(loaded.exif)
    assert exif[0x0112] == 1


def test_heic_orientation_6_is_not_rotated_twice():
    """pillow-heif stores EXIF 6 as irot and applies it on decode; we must not rotate again."""
    heic = make_heic_bytes((300, 200), orientation=6)
    loaded = io.load(heic)
    assert loaded.format == "heic"
    assert loaded.orientation == 6
    assert loaded.image.size == (200, 300)
    # Same visual orientation a viewer shows for the equivalent JPEG.
    jpeg = io.load(make_jpeg_bytes((300, 200), orientation=6))
    _assert_quadrants(loaded.image, jpeg.image, tol=20)
    exif = Image.Exif()
    exif.load(loaded.exif)
    assert exif.get(0x0112, 1) == 1


def test_heic_without_orientation_and_bit_depth():
    loaded = io.load(make_heic_bytes((300, 200)))
    assert loaded.image.size == (300, 200)
    assert loaded.orientation == 1
    assert loaded.bit_depth == 8
    assert loaded.image.mode == "RGB"
    _assert_quadrants(loaded.image, make_image((300, 200), kind="quadrants"), tol=20)


@pytest.mark.parametrize("fmt", ["PNG", "WEBP", "AVIF", "TIFF"])
def test_other_formats_apply_orientation_once(fmt):
    src = make_image((300, 200), kind="quadrants")
    data = encode(src, fmt, exif=make_exif_bytes(orientation=6), **({"lossless": True} if fmt == "WEBP" else {}))
    loaded = io.load(data)
    assert loaded.image.size == (200, 300)
    assert loaded.orientation == 6
    expected = src.transpose(Image.Transpose.ROTATE_270)
    _assert_quadrants(loaded.image, expected, tol=24)


def test_loaded_image_is_detached_from_file_metadata():
    loaded = io.load(make_jpeg_bytes(gps=True, camera=True, dpi=300))
    assert loaded.image.info == {}
    assert type(loaded.image) is Image.Image


# ------------------------------------------------------------------ normalisation
def test_palette_with_transparency_becomes_rgba():
    pal = Image.new("P", (40, 30), 0)
    pal.putpalette([255, 0, 0, 0, 255, 0] + [0] * 762)
    pal.paste(1, (20, 0, 40, 30))
    data = encode(pal, "PNG", transparency=0)
    loaded = io.load(data)
    assert loaded.image.mode == "RGBA"
    assert loaded.image.getpixel((5, 5))[3] == 0
    assert loaded.image.getpixel((30, 5)) == (0, 255, 0, 255)


def test_palette_without_transparency_becomes_rgb():
    loaded = io.load(encode(make_image((40, 30), "P"), "PNG"))
    assert loaded.image.mode == "RGB"


def test_gif_first_frame_and_frame_count():
    frames = [Image.new("RGB", (20, 10), c) for c in ("red", "green", "blue")]
    buf = stdio.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=100)
    loaded = io.load(buf.getvalue())
    assert loaded.format == "gif"
    assert loaded.frames == 3
    assert loaded.image.mode == "RGB"
    assert close_color(loaded.image.getpixel((5, 5)), (255, 0, 0), 4)


def test_multipage_tiff_first_page():
    pages = [Image.new("RGB", (20, 10), c) for c in ("blue", "red")]
    buf = stdio.BytesIO()
    pages[0].save(buf, "TIFF", save_all=True, append_images=pages[1:])
    loaded = io.load(buf.getvalue())
    assert loaded.frames == 2
    assert loaded.image.getpixel((1, 1)) == (0, 0, 255)


def test_cmyk_jpeg_becomes_rgb():
    cmyk = make_image((40, 30), "CMYK")
    loaded = io.load(encode(cmyk, "JPEG", quality=95))
    assert loaded.image.mode == "RGB"
    assert loaded.icc_profile is None


def test_16bit_grayscale_png_scaled_to_8bit():
    arr = np.zeros((10, 20), dtype=np.uint16)
    arr[:, :10] = 65535
    arr[:, 10:] = 32896  # 128 * 257
    data = encode(Image.fromarray(arr), "PNG")
    loaded = io.load(data)
    assert loaded.image.mode == "L"
    assert loaded.bit_depth == 16
    assert loaded.image.getpixel((2, 2)) == 255
    assert loaded.image.getpixel((15, 2)) == 128


def test_float_tiff_scaled_to_8bit():
    arr = np.linspace(0, 1, 200, dtype=np.float32).reshape(10, 20)
    loaded = io.load(encode(Image.fromarray(arr), "TIFF"))
    assert loaded.image.mode == "L"
    assert loaded.image.getpixel((0, 0)) == 0
    assert loaded.image.getpixel((19, 9)) == 255


def test_one_bit_becomes_l_and_la_kept():
    assert io.load(encode(make_image((16, 16), "1"), "PNG")).image.mode == "L"
    assert io.load(encode(make_image((16, 16), "LA"), "PNG")).image.mode == "LA"
    assert io.load(encode(make_image((16, 16), "RGBA"), "PNG")).image.mode == "RGBA"


def test_16bit_heic_decodes_to_8bit():
    import pillow_heif

    arr = (np.random.default_rng(0).integers(0, 65535, (40, 60, 3))).astype(np.uint16)
    heif = pillow_heif.from_bytes(mode="RGB;16", size=(60, 40), data=arr.tobytes())
    buf = stdio.BytesIO()
    heif.save(buf, quality=90)
    loaded = io.load(buf.getvalue())
    assert loaded.image.mode == "RGB"
    assert loaded.bit_depth in (10, 12, 16)


def test_ico_and_jp2_inputs():
    img = make_image((64, 64), "RGBA")
    ico = io.load(encode(img, "ICO", sizes=[(64, 64), (32, 32)]))
    assert ico.format == "ico" and ico.image.size == (64, 64)
    jp2 = io.load(encode(make_image((40, 30)), "JPEG2000"))
    assert jp2.format == "jp2" and jp2.image.size == (40, 30)


# ------------------------------------------------------------------ sources & errors
def test_load_from_path_and_file_object(tmp_path):
    data = make_jpeg_bytes()
    path = tmp_path / "x.jpg"
    path.write_bytes(data)
    assert io.load(str(path)).image.size == (300, 200)
    with open(path, "rb") as fh:
        assert io.load(fh).image.size == (300, 200)


@pytest.mark.parametrize("data", [b"", b"not an image at all", b"\x89PNG\r\n\x1a\n" + b"\0" * 40])
def test_garbage_is_415(data):
    with pytest.raises(io.ImageError) as err:
        io.load(data)
    assert err.value.status == 415


def test_truncated_jpeg_is_415():
    data = make_jpeg_bytes((400, 300), kind="noise")
    with pytest.raises(io.ImageError) as err:
        io.load(data[: len(data) // 2])
    assert err.value.status == 415


def test_unsupported_pillow_format_is_415():
    with pytest.raises(io.ImageError) as err:
        io.load(encode(make_image((10, 10)), "PPM"))
    assert err.value.status == 415


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_pixel_limit_is_413(pixel_limit):
    pixel_limit(5000)
    data = encode(make_image((100, 60)), "PNG")  # 6000 px: above the limit, below Pillow's 2× error
    with pytest.raises(io.ImageError) as err:
        io.load(data)
    assert err.value.status == 413
    huge = encode(make_image((200, 200)), "PNG")  # > 2× limit → Pillow's DecompressionBombError path
    with pytest.raises(io.ImageError) as err:
        io.load(huge)
    assert err.value.status == 413
    assert io.load(encode(make_image((50, 50)), "PNG")).image.size == (50, 50)


@pytest.mark.filterwarnings("ignore::PIL.Image.DecompressionBombWarning")
def test_pixel_limit_applies_to_heic(pixel_limit):
    data = make_heic_bytes((120, 100))
    pixel_limit(10_000)
    with pytest.raises(io.ImageError) as err:
        io.load(data)
    assert err.value.status == 413


# ------------------------------------------------------------------ dpi
@pytest.mark.parametrize("fmt,params", [("JPEG", {"dpi": (300, 300)}), ("PNG", {"dpi": (300, 300)}),
                                        ("TIFF", {"dpi": (300, 300)})])
def test_dpi_is_read(fmt, params):
    loaded = io.load(encode(make_image((30, 20)), fmt, **params))
    assert loaded.dpi == (300.0, 300.0)


def test_dpi_absent_is_none_and_exif_fallback():
    assert io.load(encode(make_image((30, 20)), "PNG")).dpi is None
    webp = encode(make_image((30, 20)), "WEBP", exif=make_exif_bytes(dpi=240))
    assert io.load(webp).dpi == (240.0, 240.0)


# ------------------------------------------------------------------ colour management
def test_icc_load_and_preview_convert(p3_profile):
    data = encode(Image.new("RGB", (32, 32), (40, 200, 60)), "PNG", icc_profile=p3_profile)
    loaded = io.load(data)
    assert loaded.icc_profile == p3_profile
    preview, mime = io.preview(loaded.image, loaded.icc_profile)
    assert mime == "image/jpeg"
    px = Image.open(stdio.BytesIO(preview)).getpixel((16, 16))
    assert px[0] < 30


def test_to_srgb_no_ops_and_alpha(p3_profile):
    img = make_image((8, 8), "RGBA")
    assert io.to_srgb(img, None) is img
    assert io.to_srgb(img, b"garbage") is img
    assert io.to_srgb(img, SRGB_ICC) is img
    gray = make_image((8, 8), "L")
    assert io.to_srgb(gray, p3_profile) is gray
    converted = io.to_srgb(img, p3_profile)
    assert converted.mode == "RGBA"
    assert np.array_equal(np.asarray(converted)[..., 3], np.asarray(img)[..., 3])


def test_garbage_icc_profile_does_not_crash():
    data = encode(make_image((16, 16)), "PNG", icc_profile=b"definitely not icc")
    loaded = io.load(data)
    assert icc_description(loaded.icc_profile) is None
    io.preview(loaded.image, loaded.icc_profile)


def test_cmyk_with_mismatched_profile_falls_back_to_plain_conversion():
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    loaded = io.load(encode(Image.new("CMYK", (20, 20), (0, 255, 255, 0)), "JPEG", icc_profile=srgb, quality=95))
    assert loaded.image.mode == "RGB"
    assert loaded.icc_profile is None
    assert close_color(loaded.image.getpixel((10, 10)), (255, 0, 0), 8)


# ------------------------------------------------------------------ preview
def test_preview_downscales_and_picks_mime():
    data, mime = io.preview(make_image((3000, 1000)), max_side=600)
    assert mime == "image/jpeg"
    assert Image.open(stdio.BytesIO(data)).size == (600, 200)
    data, mime = io.preview(make_image((100, 50), "RGBA"), max_side=1600)
    assert mime == "image/png"
    assert Image.open(stdio.BytesIO(data)).size == (100, 50)
    opaque = make_image((40, 30), "RGB").convert("RGBA")
    assert io.preview(opaque)[1] == "image/jpeg"
    assert io.preview(make_image((40, 30), "L"))[1] == "image/jpeg"
    with pytest.raises(ValueError):
        io.preview(make_image(), max_side=0)


# ------------------------------------------------------------------ nclx (CICP) colour in HEIF/AVIF
_P3_PATCH, _P3_IN_SRGB = (200, 100, 50), (215, 93, 31)  # exact P3 (sRGB curve) → sRGB, computed analytically


def _srgb_eotf(v: np.ndarray) -> np.ndarray:
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


def _srgb_oetf(v: np.ndarray) -> np.ndarray:
    v = np.clip(v, 0.0, 1.0)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * v ** (1 / 2.4) - 0.055)


def _rgb_to_xyz(prims, white) -> np.ndarray:
    def xyz(x, y):
        return np.array([x / y, 1.0, (1 - x - y) / y])
    m = np.stack([xyz(*p) for p in prims], axis=1)
    return m * np.linalg.solve(m, xyz(*white))


def _reference_to_srgb(rgb8: np.ndarray, prims, white) -> np.ndarray:
    """Linear-light matrix conversion (Bradford white adaptation) — independent of ICC/LittleCMS."""
    d65 = (0.3127, 0.3290)
    brad = np.array([[0.8951, 0.2664, -0.1614], [-0.7502, 1.7135, 0.0367], [0.0389, -0.0685, 1.0296]])

    def xyz(x, y):
        return np.array([x / y, 1.0, (1 - x - y) / y])
    adapt = np.linalg.inv(brad) @ np.diag((brad @ xyz(*d65)) / (brad @ xyz(*white))) @ brad
    srgb = _rgb_to_xyz([(0.64, 0.33), (0.30, 0.60), (0.15, 0.06)], d65)
    m = np.linalg.inv(srgb) @ adapt @ _rgb_to_xyz(prims, white)
    lin = _srgb_eotf(rgb8 / 255.0) @ m.T
    return np.round(_srgb_oetf(lin) * 255)


@pytest.mark.parametrize("code", [12, 11, 9])  # Display P3, DCI-P3, BT.2020
def test_nclx_profiles_match_linear_light_reference(code):
    name, red, green, blue, white = io._NCLX_PRIMARIES[code]
    rgb = np.random.default_rng(code).integers(0, 256, (64, 64, 3)).astype(np.uint8)
    icc = io._nclx_icc(code, 13)
    assert name in icc_description(icc)
    out = np.asarray(io.to_srgb(Image.fromarray(rgb), icc), dtype=float)
    ref = _reference_to_srgb(rgb.reshape(-1, 3).astype(float), [red, green, blue], white).reshape(rgb.shape)
    err = np.abs(out - ref)
    assert err.max() <= 1 and err.mean() < 0.05


def test_nclx_profile_only_for_wide_gamut_with_known_curves():
    assert io._nclx_icc(1, 13) is None      # sRGB
    assert io._nclx_icc(2, 2) is None       # unspecified
    assert io._nclx_icc(12, 16) is None     # PQ needs tone mapping, not a matrix
    assert io._nclx_icc(12, 18) is None     # HLG
    assert io._nclx_icc(99, 13) is None
    assert io._nclx_icc(12, 1) == io._nclx_icc(12, 13)  # BT.709 curve is rendered like sRGB
    assert "linear" in icc_description(io._nclx_icc(1, 8))


def _nclx_heic(primaries: int) -> bytes:
    buf = stdio.BytesIO()
    Image.new("RGB", (64, 64), _P3_PATCH).save(buf, "HEIF", quality=-1, chroma="444", color_primaries=primaries,
                                               transfer_characteristics=13, matrix_coefficients=0, full_range_flag=1)
    return buf.getvalue()


def test_nclx_p3_heic_is_converted_to_srgb():
    loaded = io.load(_nclx_heic(12))
    assert loaded.image.getpixel((8, 8)) == _P3_PATCH  # pixels stay as decoded; the profile describes them
    assert "P3" in icc_description(loaded.icc_profile)
    out = io.to_srgb(loaded.image, loaded.icc_profile)
    assert close_color(out.getpixel((8, 8)), _P3_IN_SRGB, 2)
    preview, _ = io.preview(loaded.image, loaded.icc_profile)
    assert close_color(Image.open(stdio.BytesIO(preview)).getpixel((8, 8)), _P3_IN_SRGB, 3)


def test_nclx_srgb_heic_is_left_alone():
    loaded = io.load(_nclx_heic(1))
    assert loaded.icc_profile is None
    assert loaded.image.getpixel((8, 8)) == _P3_PATCH
    preview, _ = io.preview(loaded.image, loaded.icc_profile)
    assert close_color(Image.open(stdio.BytesIO(preview)).getpixel((8, 8)), _P3_PATCH, 3)


def _avif_with_nclx(primaries: int, transfer: int) -> bytes:
    """Pillow's AVIF writer has no CICP options: patch the nclx 'colr' box it writes."""
    data = bytearray(encode(Image.new("RGB", (64, 64), _P3_PATCH), "AVIF", quality=100, subsampling="4:4:4"))
    pos = data.find(b"colrnclx")
    assert pos > 0
    data[pos + 8:pos + 12] = primaries.to_bytes(2, "big") + transfer.to_bytes(2, "big")
    return bytes(data)


def test_nclx_p3_avif_is_converted_to_srgb():
    plain = io.load(_avif_with_nclx(1, 13))
    assert plain.icc_profile is None
    loaded = io.load(_avif_with_nclx(12, 13))
    assert "P3" in icc_description(loaded.icc_profile)
    expected = io.to_srgb(Image.new("RGB", (1, 1), plain.image.getpixel((8, 8))), loaded.icc_profile).getpixel((0, 0))
    out = io.to_srgb(loaded.image, loaded.icc_profile).getpixel((8, 8))
    assert out == expected and close_color(out, _P3_IN_SRGB, 4)
    assert io.load(_avif_with_nclx(12, 16)).icc_profile is None  # PQ: left alone


def test_avif_box_parser_survives_garbage():
    assert io._avif_nclx(b"") is None
    assert io._avif_nclx(b"\x00\x00\x00\x10ftypavif\x00\x00\x00\x00") is None
    assert io._avif_nclx(b"\x00\x00\x00\x01meta" + b"\xff" * 8) is None
    data = _avif_with_nclx(12, 13)
    for cut in range(0, len(data), 7):
        io._avif_nclx(data[:cut])  # truncated headers never raise


# ------------------------------------------------------------------ gray ICC profiles
@pytest.mark.parametrize("gamma,expected", [(1.8, 147), (2.2, 128), (1.0, 188)])
def test_gray_profile_converted_to_srgb_gray(gamma, expected):
    from tests.conftest import make_gray_icc_profile

    icc = make_gray_icc_profile(gamma, f"Gray Gamma {gamma}")
    loaded = io.load(encode(Image.new("L", (32, 32), 128), "JPEG", quality=100, icc_profile=icc))
    assert loaded.icc_profile == icc and loaded.image.mode == "L"
    out = io.to_srgb(loaded.image, loaded.icc_profile)
    assert out.mode == "L" and abs(out.getpixel((8, 8)) - expected) <= 2
    preview, _ = io.preview(loaded.image, loaded.icc_profile)
    assert abs(Image.open(stdio.BytesIO(preview)).getpixel((8, 8)) - expected) <= 2


def test_gray_profile_keeps_alpha():
    from tests.conftest import make_gray_icc_profile

    la = Image.new("LA", (8, 8), (128, 100))
    out = io.to_srgb(la, make_gray_icc_profile(1.8))
    assert out.mode == "LA" and abs(out.getpixel((0, 0))[0] - 147) <= 1 and out.getpixel((0, 0))[1] == 100
    assert io.to_srgb(la, SRGB_ICC) is la
