"""Image inputs (app/imaging/io.py): SVG via resvg (sanitised, capped, offline), PSD/PSB, camera RAW."""
from __future__ import annotations

import gzip
import http.server
import struct
import threading

import numpy as np
import pytest
from PIL import Image

from app.imaging import io
from tests.conftest import make_dng_bytes, make_psd_bytes, make_svg


# ------------------------------------------------------------------ SVG
def test_svg_renders_with_alpha_at_a_useful_size():
    loaded = io.load(make_svg())
    assert loaded.format == "svg" and loaded.frames == 1
    assert max(loaded.image.size) == io.SVG_MIN_LONG_SIDE           # small art is enlarged, not blurred
    assert loaded.image.size == (1024, 819) == io.probe_size(make_svg())
    assert loaded.image.mode == "RGBA" and loaded.image.getpixel((0, 0))[3] == 0
    r, g, b, a = loaded.image.getpixel((512, 400))
    assert (r, a) == (255, 255) and g < 5


def test_svg_without_namespace_viewbox_only_and_svgz():
    bare = b'<svg width="40" height="20"><rect width="40" height="20" fill="blue"/></svg>'
    assert io.load(bare).image.getpixel((10, 10))[:3] == (0, 0, 255)
    view_only = make_svg(width=None, height=None, view_box="0 0 200 100")
    assert io.load(view_only).image.size == (1024, 512)
    assert io.load(gzip.compress(make_svg())).format == "svg"
    assert io.probe_size(make_svg()) == (1024, 819)


def test_svg_text_uses_the_bundled_fonts():
    svg = make_svg('<text x="5" y="60" font-family="sans-serif" font-size="50" fill="#000">Hey</text>')
    img = io.load(svg).image
    assert (np.asarray(img.getchannel("A")) > 0).sum() > 2000


def test_sanitizer_drops_scripts_handlers_and_external_references():
    svg = make_svg('<script>alert(1)</script><rect width="10" height="10" onclick="x()"/>'
                   '<image href="https://example.com/a.png" width="5" height="5"/>'
                   '<image xlink:href="file:///etc/passwd" width="5" height="5"/>'
                   '<foreignObject><div>x</div></foreignObject>'
                   '<style>@import url(http://evil/x.css); rect { fill: url(http://evil/p) }</style>'
                   '<use href="#local"/>', extra=' onload="steal()"')
    clean = io.sanitize_svg(svg)
    for bad in ("script", "onclick", "onload", "example.com", "/etc/passwd", "foreignObject", "@import", "evil"):
        assert bad not in clean, bad
    assert 'href="#local"' in clean


def test_keep_links_mode_only_removes_active_content():
    svg = make_svg('<a href="javascript:alert(1)"><rect width="1" height="1"/></a>'
                   '<image href="https://example.com/a.png" width="5" height="5"/>'
                   '<animate attributeName="href" to="javascript:alert(2)"/><script>x</script>')
    clean = io.sanitize_svg(svg, keep_links=True)
    assert "javascript" not in clean and "script" not in clean and "https://example.com/a.png" in clean


class _Recorder(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):  # noqa: N802
        _Recorder.hits.append(self.path)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def http_server():
    _Recorder.hits = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_rendering_never_fetches_urls_or_reads_local_files(http_server, tmp_path):
    secret = tmp_path / "secret.png"
    Image.new("RGB", (4, 4), (0, 255, 0)).save(secret)
    svg = make_svg(f'<image x="0" y="0" width="50" height="50" xlink:href="{http_server}/ssrf.png"/>'
                   f'<image x="50" y="0" width="50" height="50" href="{secret}"/>'
                   f'<image x="0" y="40" width="50" height="40" href="file://{secret}"/>'
                   f'<rect x="60" y="60" width="10" height="10" fill="url({http_server}/css)"/>')
    img = io.load(svg).image
    assert _Recorder.hits == []
    assert np.asarray(img.getchannel("A")).max() == 0  # nothing at all was drawn


@pytest.mark.parametrize("doc, status", [
    (b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
     b'<svg xmlns="http://www.w3.org/2000/svg"><text>&xxe;</text></svg>', 415),
    (b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
     b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;"><!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">'
     b'<!ENTITY e "&d;&d;&d;&d;&d;&d;&d;&d;&d;&d;"><!ENTITY f "&e;&e;&e;&e;&e;&e;&e;&e;&e;&e;">]>'
     b'<svg xmlns="http://www.w3.org/2000/svg"><text>&f;&f;&f;&f;&f;&f;&f;&f;</text></svg>', 415),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><rect></svg>', 415),
    (b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="100000"/>', 415),
])
def test_hostile_svg_is_refused(doc, status):
    with pytest.raises(io.ImageError) as err:
        io.load(doc)
    assert err.value.status == status


def test_huge_declared_size_is_capped_not_allocated(monkeypatch):
    monkeypatch.setattr(io, "SVG_MAX_PIXELS", 1_000_000)
    img = io.load(make_svg(width="100000", height="100000", view_box="0 0 100 100")).image
    assert img.width * img.height <= 1_000_000 and img.width == img.height


def test_element_and_byte_budgets(monkeypatch):
    monkeypatch.setattr(io, "SVG_MAX_ELEMENTS", 50)
    with pytest.raises(io.ImageError) as err:
        io.load(make_svg("<g/>" * 60))
    assert err.value.status == 413
    monkeypatch.setattr(io, "SVG_MAX_BYTES", 100)
    with pytest.raises(io.ImageError) as err:
        io.load(make_svg("<!--" + "x" * 200 + "-->"))
    assert err.value.status == 413


def test_render_svg_at_a_requested_width():
    img = io.render_svg(make_svg(), width=200)
    assert img.size == (200, 160)


# ------------------------------------------------------------------ SVG font fallbacks (img-09)
@pytest.mark.parametrize("family, expected", [
    ("Arial", "Arial, sans-serif"),
    ("Helvetica", "Helvetica, sans-serif"),
    ("'Roboto', Verdana", "'Roboto', Verdana, sans-serif"),
    ("ArialMT", "ArialMT, sans-serif"),
    ("Times New Roman", "Times New Roman, serif"),
    ("Georgia", "Georgia, serif"),
    ("'Courier New'", "'Courier New', monospace"),
    ("system-ui", "system-ui, sans-serif"),
    ("Arial, sans-serif", "Arial, sans-serif"),
    ("serif", "serif"),
    ("inherit", "inherit"),
])
def test_font_families_without_a_generic_get_one(family, expected):
    clean = io.sanitize_svg(make_svg(f'<text font-family="{family}">x</text>'))
    assert f'font-family="{expected}"' in clean


def test_font_families_in_style_attributes_and_style_sheets():
    clean = io.sanitize_svg(make_svg(
        '<style>text { font: bold 20px Verdana; fill: red } .t { font-family: "Times" }</style>'
        '<text style="font-size:20px;font-family:Helvetica">x</text>'))
    assert "font: bold 20px Verdana, sans-serif;" in clean
    assert 'font-family: "Times", serif' in clean
    assert "font-family:Helvetica, sans-serif" in clean
    kept = io.sanitize_svg(make_svg('<text font-family="Arial">x</text>'), keep_links=True)
    assert 'font-family="Arial"' in kept  # an optimised SVG handed back keeps the author's fonts


def test_named_sans_fonts_render_sans_not_serif():
    def alpha(family: str) -> np.ndarray:
        svg = make_svg(f'<text x="2" y="60" font-family="{family}" font-size="40">Rag</text>')
        return np.asarray(io.load(svg).image.getchannel("A")) > 127

    sans, serif = alpha("sans-serif"), alpha("serif")
    assert (sans != serif).sum() > 500
    for family in ("Arial", "Helvetica", "Verdana", "Roboto"):
        assert (alpha(family) != sans).sum() == 0, family


# ------------------------------------------------------------------ SVG render cost (img-07)
def _turbulence_svg(n: int, side: int, octaves: int = 1) -> bytes:
    rects = "".join(f'<rect width="{side}" height="{side}" filter="url(#t{i % 2})"/>' for i in range(n))
    defs = "".join(f'<filter id="t{i}" x="0" y="0" width="1" height="1"><feTurbulence baseFrequency="0.0{i + 1}" '
                   f'numOctaves="{octaves}"/></filter>' for i in range(2))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{side}" height="{side}"><defs>{defs}</defs>{rects}'
            '</svg>').encode()


def test_filter_heavy_svg_is_refused_before_rendering(monkeypatch):
    calls = []
    monkeypatch.setattr(io, "_isolated", lambda *a, **k: calls.append(k) or b"")
    for doc in (_turbulence_svg(8, 4000), _turbulence_svg(1, 6000, octaves=10),
                make_svg('<defs><filter id="m"><feMorphology radius="30"/></filter></defs>'
                         '<rect width="100" height="80" filter="url(#m)"/>')):
        with pytest.raises(io.ImageError) as err:
            io.load(doc)
        assert err.value.status == 413 and "filter effects" in err.value.args[0]
    assert calls == []  # refused from the markup: resvg never ran


def test_ordinary_filters_still_render():
    # Figma-style drop shadows (userSpaceOnUse regions) on 100 small shapes, Inkscape-style blurs on 80 small
    # paths and groups (objectBoundingBox regions read from the geometry) and one blurred circle
    shadow = ('<filter id="s" x="0" y="0" width="10" height="10" filterUnits="userSpaceOnUse">'
              '<feGaussianBlur stdDeviation="2"/><feOffset dx="2" dy="2"/><feMerge><feMergeNode/>'
              '<feMergeNode in="SourceGraphic"/></feMerge></filter>'
              '<filter id="b"><feGaussianBlur stdDeviation="3"/></filter>')
    shapes = "".join(f'<rect x="{(i % 10) * 10}" y="{(i // 10) * 8}" width="6" height="5" filter="url(#s)"/>'
                     for i in range(100))
    paths = "".join(f'<path d="M{i % 10 * 10} {i // 10 * 8}h4v3c1 1 2 2 -2 2z" filter="url(#b)"/>'
                    for i in range(60))
    groups = "".join(f'<g filter="url(#b)"><path d="m{i * 4} 70 l3 0 0 3z"/><circle cx="{i * 4}" cy="74" r="1"/></g>'
                     for i in range(20))
    svg = make_svg(f'<defs>{shadow}</defs>{shapes}{paths}{groups}<circle cx="50" cy="40" r="20" filter="url(#b)"/>')
    root = io.ET.fromstring(io.sanitize_svg(svg))
    assert io.svg_filter_cost(root, (1024, 819)) < 1.0
    assert io.load(svg).image.size == (1024, 819)


def test_path_bounding_boxes():
    assert io._path_bbox("M10 20 L30 40 Z") == (10, 20, 30, 40)
    assert io._path_bbox("m10 20 5 5 h10 v-30") == (10, -5, 25, 25)   # implicit lineto, relative h/v
    assert io._path_bbox("M0 0 C 10 -5 20 5 30 0") == (0, -5, 30, 5)
    assert io._path_bbox("M0 0 A 5 5 0 0 1 10 0") == (0, -10, 20, 10)
    assert io._path_bbox("") is None and io._path_bbox("X 1 2") is None


def test_turbulence_octaves_are_clamped_and_renders_use_the_short_timeout(monkeypatch):
    clean = io.sanitize_svg(_turbulence_svg(1, 100, octaves=12))
    assert f'numOctaves="{io.SVG_MAX_OCTAVES}"' in clean and 'numOctaves="12"' not in clean
    assert 'numOctaves="12"' in io.sanitize_svg(_turbulence_svg(1, 100, octaves=12), keep_links=True)
    seen = {}
    real = io._isolated

    def spy(fn, *args, **kwargs):
        seen.update(kwargs)
        return real(fn, *args, **kwargs)

    monkeypatch.setattr(io, "_isolated", spy)
    io.load(make_svg())
    assert seen["timeout_s"] == io.SVG_RENDER_TIMEOUT_S <= 15


def test_css_and_style_filters_are_counted():
    rects = '<rect class="fx" width="4000" height="4000"/>' * 6
    doc = ('<svg xmlns="http://www.w3.org/2000/svg" width="4000" height="4000">'
           '<style>.fx { filter: url(#t) }</style>'
           '<defs><filter id="t"><feTurbulence numOctaves="4"/></filter></defs>' + rects + '</svg>')
    root = io.ET.fromstring(io.sanitize_svg(doc.encode()))
    assert io.svg_filter_cost(root, (4000, 4000)) > io.SVG_FILTER_BUDGET_S
    inline = doc.replace('class="fx"', 'style="filter:url(#t)"').replace(".fx { filter: url(#t) }", "")
    assert io.svg_filter_cost(io.ET.fromstring(inline), (4000, 4000)) > io.SVG_FILTER_BUDGET_S


def test_an_uploaded_svg_is_rendered_once(tmp_path, monkeypatch):
    from app.store import FileStore

    store = FileStore(tmp_path / "files", ttl=600)
    store.init()
    stored = store.save("art.svg", __import__("io").BytesIO(make_svg()), max_bytes=10_000_000)
    first = store.load(stored.id)
    store.set_info(stored.id, width=first.image.width, height=first.image.height, format=first.format)
    assert store.read_aux(stored.id, io.SVG_RASTER_AUX)
    store.evict(stored.id)
    monkeypatch.setattr(io, "render_svg", lambda *a, **k: pytest.fail("the SVG was rendered again"))
    again = store.load(stored.id)
    assert again.format == "svg" and again.image.size == first.image.size and again.image.mode == first.image.mode
    assert np.array_equal(np.asarray(again.image), np.asarray(first.image))


# ------------------------------------------------------------------ PSD / PSB
def test_8bit_psd_uses_the_merged_composite_and_one_frame():
    loaded = io.load(make_psd_bytes(color=(0.0, 0.0, 1.0), layers=2))
    assert loaded.format == "psd" and loaded.frames == 1 and loaded.bit_depth == 8
    assert loaded.image.getpixel((20, 20))[:3] == (0, 0, 255)


@pytest.mark.parametrize("depth", [16, 32])
def test_deep_psd_goes_through_psd_tools(depth):
    loaded = io.load(make_psd_bytes(depth=depth, color=(1.0, 0.0, 0.0)))
    assert loaded.format == "psd" and loaded.bit_depth == depth and loaded.image.mode == "RGB"
    assert loaded.image.getpixel((5, 5)) == (255, 0, 0)


def test_psb_large_document_format():
    data = make_psd_bytes(psb=True, color=(0.0, 1.0, 0.0))
    assert io.sniff(data[:64]) == "psb"
    loaded = io.load(data)
    assert loaded.format == "psb" and loaded.image.getpixel((5, 5))[:3] == (0, 255, 0)


def test_psd_header_bomb_is_refused_before_decoding():
    header = b"8BPS" + struct.pack(">H6xHIIHH", 1, 3, 100_000, 100_000, 8, 3) + b"\0" * 64
    with pytest.raises(io.ImageError) as err:
        io.load(header)
    assert err.value.status == 413
    assert io.probe_size(header) == (100_000, 100_000)


def test_layered_psd_without_a_merged_image_is_composited_from_layers():
    good = make_psd_bytes(color=(0.0, 0.0, 1.0), layers=2)
    blank = make_psd_bytes(color=(1.0, 1.0, 1.0))

    def image_data(d: bytes) -> int:  # offset of the merged image data (the last PSD section)
        off = 26
        for _ in range(3):  # colour mode data, image resources, layer and mask info
            off += 4 + struct.unpack(">I", d[off:off + 4])[0]
        return off

    franken = good[:image_data(good)] + blank[image_data(blank):]
    from PIL import Image as PILImage
    import io as _io
    assert PILImage.open(_io.BytesIO(franken)).getpixel((20, 20))[:3] == (255, 255, 255)
    assert io.load(franken).image.getpixel((20, 20))[:3] == (0, 0, 255)


def test_corrupt_psd_is_415():
    data = make_psd_bytes()
    with pytest.raises(io.ImageError) as err:
        io.load(data[:40])
    assert err.value.status == 415


# ------------------------------------------------------------------ camera RAW
def test_dng_is_sniffed_and_demosaiced_by_libraw_not_pillow():
    data = make_dng_bytes((64, 48))
    assert io.sniff(data[:4096]) == "dng"
    loaded = io.load(data)
    assert loaded.format == "dng" and loaded.image.size == (64, 48) and loaded.image.mode == "RGB"
    left, right = loaded.image.getpixel((4, 24)), loaded.image.getpixel((60, 24))
    assert sum(right) > sum(left) + 60              # the sensor's exposure ramp, decoded from the CFA data
    assert loaded.exif is not None and b"heypdf" in loaded.exif


def test_raw_orientation_is_applied_once():
    loaded = io.load(make_dng_bytes((64, 48), orientation=6))
    assert loaded.image.size == (48, 64) and loaded.orientation == 6


def test_raw_above_the_pixel_limit_is_413(pixel_limit):
    pixel_limit(1000)
    with pytest.raises(io.ImageError) as err:
        io.load(make_dng_bytes((64, 48)))
    assert err.value.status == 413


@pytest.mark.parametrize("head, kind", [
    (b"FUJIFILMCCD-RAW 0201" + b"\0" * 40, "raf"), (b"\0\0\0\x18ftypcrx " + b"\0" * 40, "cr3"),
    (b"IIRO" + b"\0" * 40, "orf"), (b"IIU\0" + b"\0" * 40, "rw2"), (b"II*\0\x10\0\0\0CR\x02\0" + b"\0" * 40, "cr2"),
])
def test_raw_magic_bytes(head, kind):
    assert io.sniff(head) == kind


def test_corrupt_raw_is_415():
    with pytest.raises(io.ImageError) as err:
        io.load(b"FUJIFILMCCD-RAW 0201" + b"\x01" * 2000)
    assert err.value.status == 415


def test_plain_tiff_is_still_pillows():
    import io as _io

    buf = _io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, "TIFF")
    assert io.sniff(buf.getvalue()) is None
    assert io.load(buf.getvalue(), name="photo.tif").format == "tiff"
