"""heypdf Scan to PDF: edge detection, straightening, QR hand-off sessions (phone upload, expiry, abuse)."""
from __future__ import annotations

import io
import time

import numpy as np
import pymupdf
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.pdf import qr, scan
from app.pdf.errors import PdfError
from app.pdf.routes import SESSIONS, SessionStore, scan_router
from app.store import FileStore
from tests.fixtures.pdf.factory import pdfs, run_tool  # noqa: F401  (session fixture)


# ------------------------------------------------------------------ engine
def test_detects_the_document_corners(pdfs):
    found = scan.detect_corners(str(pdfs.photo), "photo.jpg")
    assert found["corners"] is not None
    err = [abs(a - b) for got, want in zip(found["corners"], pdfs.photo_corners, strict=True)
           for a, b in zip(got, want, strict=True)]
    assert max(err) < 0.02, err  # within 2 % of the image size


def test_scan_straightens_into_a_pdf(pdfs, tmp_path):
    ctx = run_tool("pdf.scan", tmp_path, [(pdfs.photo, "image")], {"enhance": "bw", "page_size": "a4"})
    doc = pymupdf.open(ctx.outputs[0][0])
    assert doc.page_count == 1 and doc[0].rect.width == pytest.approx(595.3, abs=1)
    assert ctx.data["images"][0]["method"] == "detected"
    assert ctx.outputs[0][1] == "photo_scan.pdf"
    # the straightened page is mostly paper: white with dark text lines
    pix = doc[0].get_pixmap(dpi=30, colorspace=pymupdf.csGRAY)
    arr = np.frombuffer(pix.samples, np.uint8)
    assert arr.mean() > 200


def test_scan_with_manual_corners_rotation_and_no_detection(pdfs, tmp_path):
    corners = [[[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]]
    ctx = run_tool("pdf.scan", tmp_path, [(pdfs.photo, "image"), (pdfs.jpeg, "image")],
                   {"corners": corners + [None], "rotate": [90, 0], "detect": False, "page_size": "fit",
                    "enhance": "none"})
    doc = pymupdf.open(ctx.outputs[0][0])
    assert doc.page_count == 2
    assert doc[0].rect.width > doc[0].rect.height  # the portrait crop was turned 90°
    assert [i["method"] for i in ctx.data["images"]] == ["manual", "full"]


def _photo_of_a4(tilt_deg: float, yaw_deg: float = 0.0, size=(1500, 2000), fov_deg: float = 65.0,
                 distance_m: float = 0.45):
    """Corners (tl, tr, br, bl, pixels) of an A4 sheet photographed by a pinhole camera (principal
    point at the centre, square pixels) tilted by ``tilt_deg`` about the x axis and ``yaw_deg`` about y."""
    w, h = size
    f = (max(w, h) / 2) / np.tan(np.radians(fov_deg) / 2)
    a, b = np.radians(tilt_deg), np.radians(yaw_deg)
    rx = np.array([[1, 0, 0], [0, np.cos(a), -np.sin(a)], [0, np.sin(a), np.cos(a)]])
    ry = np.array([[np.cos(b), 0, np.sin(b)], [0, 1, 0], [-np.sin(b), 0, np.cos(b)]])
    pw, ph = 0.210, 0.297
    corners = np.array([[-pw / 2, -ph / 2, 0], [pw / 2, -ph / 2, 0], [pw / 2, ph / 2, 0], [-pw / 2, ph / 2, 0]])
    cam = corners @ (ry @ rx).T + np.array([0, 0, distance_m])
    return np.float32([[f * x / z + w / 2, f * y / z + h / 2] for x, y, z in cam])


@pytest.mark.parametrize(("tilt", "yaw"), [(0, 0), (10, 0), (20, 5), (30, 0), (25, -10)])
def test_scan_restores_the_page_proportions(tilt, yaw):
    """An angled photo of an A4 page straightens to A4's 1 : 1.414, not the foreshortened ratio."""
    quad = _photo_of_a4(tilt, yaw)
    w, h = 1500, 2000
    assert (quad >= 0).all() and (quad[:, 0] < w).all() and (quad[:, 1] < h).all()
    aspect = scan.estimate_aspect(quad, w, h)
    assert aspect is not None and abs((1 / aspect) / 1.4142 - 1) < 0.03, (tilt, yaw, aspect)
    out = scan.warp(np.full((h, w, 3), 255, np.uint8), quad)
    assert abs(out.shape[0] / out.shape[1] / 1.4142 - 1) < 0.03, (tilt, yaw, out.shape)


def test_scan_warp_falls_back_and_pads_with_white():
    quad = np.float32([[100, 100], [900, 120], [880, 1300], [90, 1280]])  # a slightly skewed quad
    img = np.zeros((1400, 1000, 3), np.uint8)
    out = scan.warp(img, np.float32([[-50, -50], [1049, -50], [1049, 1449], [-50, 1449]]))
    assert out[0, 0].tolist() == [255, 255, 255]  # outside the photo: paper white, not smeared edges
    assert scan.warp(img, quad).shape[0] > 0
    assert scan.estimate_aspect(np.float32([[0, 0], [10, 0], [10, 10], [0, 10]]), 100, 100) == pytest.approx(1.0)


def test_scan_rejects_degenerate_corners_and_too_many_entries(pdfs, tmp_path):
    with pytest.raises(PdfError, match="enclose almost nothing"):
        run_tool("pdf.scan", tmp_path, [(pdfs.photo, "image")],
                 {"corners": [[[0.5, 0.5], [0.51, 0.5], [0.51, 0.51], [0.5, 0.51]]]})
    with pytest.raises(PdfError, match="more corner"):
        run_tool("pdf.scan", tmp_path, [(pdfs.photo, "image")], {"rotate": [0, 90]})


def test_scan_refuses_non_images(pdfs, tmp_path):
    with pytest.raises(PdfError):
        run_tool("pdf.scan", tmp_path, [(pdfs.report, "image")], {})


# ------------------------------------------------------------------ QR
@pytest.mark.parametrize("text", ["https://heypdf.example.org/phone#AbCdEfGhIjKlMnOpQrStUv", "x" * 150])
def test_qr_code_decodes(text):
    import cv2

    m = np.array(qr.matrix(text), dtype=np.uint8)
    img = np.pad(np.where(m, 0, 255).astype(np.uint8), 4, constant_values=255)
    img = cv2.resize(img, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
    value, _pts, _ = cv2.QRCodeDetector().detectAndDecode(img)
    assert value == text
    svg = qr.svg(text)
    assert svg.startswith("<svg") and "<script" not in svg


def test_qr_rejects_overlong_text():
    with pytest.raises(ValueError):
        qr.matrix("x" * 300)


# ------------------------------------------------------------------ sessions API
@pytest.fixture()
def client(tmp_path):
    settings = Settings(tmp_dir=str(tmp_path / "store"), public_url="https://heypdf.example.org")
    store = FileStore(settings.tmp_dir, 3600)
    store.init()
    app = FastAPI()
    app.state.settings = settings
    app.state.store = store
    app.include_router(scan_router, prefix="/api")
    SESSIONS.clear()
    with TestClient(app) as c:
        c.store = store  # type: ignore[attr-defined]
        yield c
    SESSIONS.clear()


def test_session_round_trip(client, pdfs):
    r = client.post("/api/scan/sessions")
    assert r.status_code == 201
    body = r.json()
    sid = body["id"]
    assert len(sid) >= 22 and body["url"] == f"https://heypdf.example.org/phone#{sid}"
    assert body["qr_svg"].startswith("<svg") and body["reachable_from_phone"] is True
    assert body["expires"] - time.time() == pytest.approx(15 * 60, abs=5)
    files = [("files", ("page1.jpg", pdfs.photo.read_bytes(), "image/jpeg")),
             ("files", ("page2.jpg", pdfs.jpeg.read_bytes(), "image/jpeg"))]
    up = client.post(f"/api/scan/sessions/{sid}/pages", files=files)
    assert up.status_code == 201, up.text
    status = client.get(f"/api/scan/sessions/{sid}").json()
    assert status["count"] == 2
    page = status["pages"][0]
    assert page["kind"] == "image" and page["format"] == "jpeg" and page["width"] == 1600
    det = client.get(f"/api/scan/detect/{page['id']}")
    assert det.status_code == 200 and det.json()["corners"] is not None
    assert client.delete(f"/api/scan/sessions/{sid}").status_code == 204
    assert client.get(f"/api/scan/sessions/{sid}").status_code == 404


def test_session_rejects_non_photos_and_cleans_up(client, pdfs):
    sid = client.post("/api/scan/sessions").json()["id"]
    before = set(client.store.ids())
    files = [("files", ("ok.jpg", pdfs.jpeg.read_bytes(), "image/jpeg")),
             ("files", ("evil.jpg", pdfs.report.read_bytes(), "image/jpeg"))]
    r = client.post(f"/api/scan/sessions/{sid}/pages", files=files)
    assert r.status_code == 415
    assert set(client.store.ids()) == before  # nothing half-stored
    assert client.get(f"/api/scan/sessions/{sid}").json()["count"] == 0


def test_unknown_and_expired_sessions(client, pdfs, monkeypatch):
    assert client.get("/api/scan/sessions/not-a-real-session-id").status_code == 404
    files = [("files", ("a.jpg", pdfs.jpeg.read_bytes(), "image/jpeg"))]
    assert client.post("/api/scan/sessions/AAAAAAAAAAAAAAAAAAAAAA/pages", files=files).status_code == 404
    sid = client.post("/api/scan/sessions").json()["id"]
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 16 * 60)
    assert client.get(f"/api/scan/sessions/{sid}").status_code == 404


def test_session_page_cap(client, pdfs, monkeypatch):
    from app.pdf import routes

    monkeypatch.setattr(routes, "MAX_SESSION_PAGES", 2)
    sid = client.post("/api/scan/sessions").json()["id"]
    files = [("files", (f"{i}.jpg", pdfs.jpeg.read_bytes(), "image/jpeg")) for i in range(3)]
    assert client.post(f"/api/scan/sessions/{sid}/pages", files=files).status_code == 413


def test_session_store_is_bounded():
    store = SessionStore(max_sessions=2)
    store.create()
    store.create()
    with pytest.raises(PdfError) as exc:
        store.create()
    assert exc.value.status == 503
    per_client = SessionStore()
    for _ in range(5):
        per_client.create("client-a")
    with pytest.raises(PdfError) as exc:
        per_client.create("client-a")
    assert exc.value.status == 429
    assert per_client.create("client-b")


def test_local_url_is_flagged(tmp_path):
    settings = Settings(tmp_dir=str(tmp_path / "s"))
    store = FileStore(settings.tmp_dir, 3600)
    store.init()
    app = FastAPI()
    app.state.settings, app.state.store = settings, store
    app.include_router(scan_router, prefix="/api")
    with TestClient(app, base_url="http://127.0.0.1:8080") as c:
        body = c.post("/api/scan/sessions").json()
    assert body["url"].startswith("http://127.0.0.1:8080/phone#") and body["reachable_from_phone"] is False
    SESSIONS.clear()


def test_upload_scan_page_png_from_bytes(client):
    sid = client.post("/api/scan/sessions").json()["id"]
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (255, 255, 255)).save(buf, "PNG")
    r = client.post(f"/api/scan/sessions/{sid}/pages", files=[("files", ("p.png", buf.getvalue(), "image/png"))])
    assert r.status_code == 201 and r.json()["pages"][0]["format"] == "png"
