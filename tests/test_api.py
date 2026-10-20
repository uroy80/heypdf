"""HTTP API (PLAN.md §3): health, uploads, file info / preview / download / delete and request admission."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from io import BytesIO

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import api as api_module
from app.api import content_disposition
from app.config import VERSION, Settings
from app.imaging import io as imgio
from app.main import CSP, create_app

ID_RE = re.compile(r"^[0-9a-f]{32}$")


# ------------------------------------------------------------------ helpers / fixtures
def encode(img: Image.Image, fmt: str = "JPEG", **kw) -> bytes:
    buf = BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def photo(w: int = 320, h: int = 240, seed: int = 0) -> Image.Image:
    """A textured RGB image (gradients + noise) so encoders behave like on photos."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w]
    arr = np.stack([x * 255 // max(1, w - 1), y * 255 // max(1, h - 1), (x + y) % 256], axis=-1).astype(np.int16)
    arr += rng.integers(-12, 12, arr.shape, dtype=np.int16)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")


def pdf_bytes(text: str = "one page") -> bytes:
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    return doc.tobytes()


def heic_bytes(img: Image.Image) -> bytes:
    import pillow_heif

    pillow_heif.register_heif_opener()
    return encode(img, "HEIF", quality=90)


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(tmp_dir=str(tmp_path / "heypdf"), max_upload_mb=5, max_files=10, max_jobs=2,
                    cache_items=4, preview_max=800, ttl_minutes=60)


@pytest.fixture()
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def upload(client, *files: tuple[str, bytes], mime: str = "application/octet-stream"):
    return client.post("/api/files", files=[("files", (name, data, mime)) for name, data in files])


def upload_ok(client, *files: tuple[str, bytes]) -> list[dict]:
    res = upload(client, *files)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["errors"] == []
    return body["files"]


# ------------------------------------------------------------------ static endpoints
def test_health(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok" and body["version"] == VERSION == "1.0.0"
    assert set(body["features"]) == {"heic", "avif", "pdf", "docs", "renderer", "ai"}
    assert all(isinstance(v, bool) for v in body["features"].values())
    assert body["features"]["heic"] is True and body["features"]["pdf"] is True
    assert body["limits"] == {"max_upload_mb": 5, "max_files": 10,
                              "max_pixels": min(50_000_000, imgio.max_pixels())}  # the stricter limit


def test_security_headers(client):
    res = client.get("/api/health")
    assert res.headers["content-security-policy"] == CSP
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["referrer-policy"] == "no-referrer"


def test_static_ui_served_with_revalidation(client):
    index = client.get("/")
    assert index.status_code == 200 and index.headers["content-type"].startswith("text/html")
    assert index.headers["cache-control"] == "no-cache, no-transform"
    assert index.headers["content-security-policy"] == CSP
    assert "<title>heypdf :)" in index.text
    script = client.get("/js/main.js")
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]
    assert script.headers["cache-control"] == "no-cache"
    assert "cache-control" not in client.get("/api/health").headers


# ------------------------------------------------------------------ upload
def test_upload_jpeg_png_heic(client):
    files = upload_ok(client, ("a.jpg", encode(photo(320, 240))),
                      ("b.png", encode(photo(100, 50), "PNG")),
                      ("IMG_0001.HEIC", heic_bytes(photo(160, 120))))
    assert [f["name"] for f in files] == ["a.jpg", "b.png", "IMG_0001.HEIC"]
    assert [f["kind"] for f in files] == ["image"] * 3
    assert [f["format"] for f in files] == ["jpeg", "png", "heic"]
    assert [f["mime"] for f in files] == ["image/jpeg", "image/png", "image/heic"]
    assert [(f["width"], f["height"]) for f in files] == [(320, 240), (100, 50), (160, 120)]
    for f in files:
        assert ID_RE.fullmatch(f["id"])
        assert f["size_bytes"] > 0 and f["source"] == "upload" and "exif" not in f


def test_upload_records_the_oriented_size(client):
    exif = Image.Exif()
    exif[0x0112] = 6
    (f,) = upload_ok(client, ("rot.jpg", encode(photo(200, 100), exif=exif.tobytes())))
    assert (f["width"], f["height"]) == (100, 200)


def test_upload_partial_failure_reports_errors(client):
    res = upload(client, ("good.png", encode(photo(), "PNG")), ("notes.bin", b"\x00\x01hello, not an image"))
    assert res.status_code == 200
    body = res.json()
    assert len(body["files"]) == 1
    assert body["errors"] == [{"name": "notes.bin", "detail": body["errors"][0]["detail"]}]
    assert body["errors"][0]["detail"]
    assert len(client.app.state.store.ids()) == 1  # the bad upload was cleaned up


def test_upload_all_bad_is_415(client):
    res = upload(client, ("x.jpg", b"\xff\xd8\xff garbage"), ("y.heic", b"nope"))
    assert res.status_code == 415
    body = res.json()
    assert body["detail"] and body["files"] == [] and len(body["errors"]) == 2
    assert client.app.state.store.ids() == []


def test_upload_oversize_is_413(tmp_path):
    settings = Settings(tmp_dir=str(tmp_path / "s"), max_upload_mb=1, max_files=5)
    with TestClient(create_app(settings)) as c:
        big = os.urandom(1024 * 1024 + 10)
        res = upload(c, ("huge.pdf", big))
        assert res.status_code == 413
        assert "limit" in res.json()["detail"]
        assert res.json()["errors"][0]["name"] == "huge.pdf"
        assert c.app.state.store.ids() == []
        ok = upload(c, ("huge.pdf", big), ("small.png", encode(photo(), "PNG")))
        assert ok.status_code == 200 and len(ok.json()["files"]) == 1 and len(ok.json()["errors"]) == 1


def test_upload_empty_file_is_400(client):
    res = upload(client, ("empty.pdf", b""))
    assert res.status_code == 400


def test_upload_too_many_files(client):
    res = upload(client, *[(f"{i}.png", b"x") for i in range(11)])
    assert res.status_code == 413
    assert "Too many files" in res.json()["detail"]


def test_upload_requires_files_field(client):
    res = client.post("/api/files", data={"x": "1"})
    assert res.status_code == 422
    assert isinstance(res.json()["detail"], str)


def test_upload_name_is_sanitised(client):
    (f,) = upload_ok(client, ("../../etc/passwd.png", encode(photo(), "PNG")))
    assert f["name"] == "passwd.png"


def test_too_many_pixels_is_413(tmp_path):
    """The app's own Settings drive the pixel limit (not the process environment)."""
    with TestClient(create_app(Settings(tmp_dir=str(tmp_path / "s"), max_pixels=1_000_000))) as c:
        upload_ok(c, ("a.png", encode(photo(200, 200), "PNG")))
        res = upload(c, ("big.png", encode(photo(1200, 1000), "PNG")))
        assert res.status_code == 413 and "above the 1 MP limit" in res.json()["detail"]
        assert len(c.app.state.store.ids()) == 1  # the refused upload is not kept
        assert c.get("/api/health").json()["limits"]["max_pixels"] == 1_000_000


# ------------------------------------------------------------------ file endpoints
def test_get_info_preview_and_delete(client):
    (f,) = upload_ok(client, ("a.jpg", encode(photo(400, 300))))
    info = client.get(f"/api/files/{f['id']}")
    assert info.status_code == 200 and info.json() == f

    thumb = client.get(f"/api/files/{f['id']}/preview", params={"max": 100})
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/jpeg"
    assert thumb.headers["cache-control"] == "private, max-age=3600"
    with Image.open(BytesIO(thumb.content)) as im:
        assert max(im.size) <= 100
    again = client.get(f"/api/files/{f['id']}/preview", params={"max": 100})
    assert again.content == thumb.content

    big = client.get(f"/api/files/{f['id']}/preview", params={"max": 5000})
    with Image.open(BytesIO(big.content)) as im:
        assert max(im.size) <= 800  # clamped to HEYP_PREVIEW_MAX

    assert client.delete(f"/api/files/{f['id']}").status_code == 204
    assert client.get(f"/api/files/{f['id']}").status_code == 404
    assert client.get(f"/api/files/{f['id']}/preview").status_code == 404
    assert client.delete(f"/api/files/{f['id']}").status_code == 404


def test_preview_png_for_alpha(client):
    img = Image.new("RGBA", (64, 64), (255, 0, 0, 0))
    (f,) = upload_ok(client, ("t.png", encode(img, "PNG")))
    res = client.get(f"/api/files/{f['id']}/preview")
    assert res.headers["content-type"] == "image/png"


def test_preview_of_a_document_is_415(client):
    (f,) = upload_ok(client, ("doc.pdf", pdf_bytes()))
    res = client.get(f"/api/files/{f['id']}/preview")
    assert res.status_code == 415 and "not an image" in res.json()["detail"]


def test_preview_cache_only_keeps_bucket_sizes(client):
    """Arbitrary ?max values are rendered but never written to the upload's directory."""
    (f,) = upload_ok(client, ("a.jpg", encode(photo(400, 300))))
    folder = client.app.state.store.root / f["id"]
    for side in (100, 101, 377, 240, 800):  # 800 = this app's preview_max
        res = client.get(f"/api/files/{f['id']}/preview", params={"max": side})
        assert res.status_code == 200
        with Image.open(BytesIO(res.content)) as im:
            assert max(im.size) <= side
    assert sorted(p.name for p in folder.glob("preview-*")) == ["preview-240.jpg", "preview-800.jpg"]


@pytest.mark.parametrize("bad", ["nothex", "g" * 32, "A" * 32, "0" * 33, "0" * 31 + "%0A"])
def test_invalid_path_ids_are_404(client, bad):
    assert client.get(f"/api/files/{bad}").status_code == 404
    assert client.delete(f"/api/files/{bad}").status_code == 404


def test_unknown_and_expired_ids(client):
    assert client.get("/api/files/" + "0" * 32).status_code == 404
    (f,) = upload_ok(client, ("a.png", encode(photo(), "PNG")))
    meta = client.app.state.store.root / f["id"] / "meta.json"
    old = time.time() - 3 * 3600
    os.utime(meta, (old, old))
    assert client.get(f"/api/files/{f['id']}").status_code == 404


def test_startup_wipes_previous_uploads(tmp_path):
    root = tmp_path / "store"
    stale = root / ("a" * 32)
    stale.mkdir(parents=True)
    (stale / "source").write_bytes(b"old")
    with TestClient(create_app(Settings(tmp_dir=str(root)))):
        assert not stale.exists()


def test_content_disposition():
    cd = content_disposition("résumé \"final\".pdf")
    assert cd == ("attachment; filename=\"resume _final_.pdf\"; "
                  "filename*=UTF-8''r%C3%A9sum%C3%A9%20%22final%22.pdf")
    assert content_disposition("報告.pdf").startswith('attachment; filename="heypdf.pdf";')
    assert content_disposition("x/y;z.pdf").startswith('attachment; filename="x_y_z.pdf";')


# ------------------------------------------------------------------ errors and request caps
def test_unexpected_errors_keep_security_headers(tmp_path):
    """The generic 500 handler runs outside the middleware, so it sets the headers itself."""
    app = create_app(Settings(tmp_dir=str(tmp_path / "s")))

    @app.get("/api/boom")
    def boom() -> None:
        raise RuntimeError("boom")

    app.router.routes.insert(0, app.router.routes.pop())  # before the static mount
    with TestClient(app, raise_server_exceptions=False) as c:
        res = c.get("/api/boom")
    assert res.status_code == 500 and res.json() == {"detail": "Internal server error"}
    assert res.headers["content-security-policy"] == CSP
    assert res.headers["x-content-type-options"] == "nosniff" and res.headers["referrer-policy"] == "no-referrer"


def test_json_body_over_the_cap_is_413(client):
    """Oversized JSON is refused before it is read and parsed."""
    body = json.dumps({"tool": "pdf.merge", "file_ids": [], "options": {"x": "x" * (3 * 1024 * 1024)}})
    res = client.post("/api/jobs", content=body, headers={"content-type": "application/json"})
    assert res.status_code == 413 and "too large" in res.json()["detail"]
    assert res.headers["content-security-policy"] == CSP


def test_upload_body_over_the_cap_is_413(tmp_path, monkeypatch):
    """A multipart body above the request cap is refused before it is spooled or stored."""
    import app.config as config_module

    monkeypatch.setattr(config_module, "UPLOAD_REQUEST_MIN_MB", 1)
    with TestClient(create_app(Settings(tmp_dir=str(tmp_path / "s"), max_upload_mb=1))) as c:
        assert c.app.state.settings.max_request_bytes == 2 * 1024 * 1024
        res = upload(c, ("a.bin", os.urandom(1024 * 1024)), ("b.bin", os.urandom(1024 * 1024 + 10)))
        assert res.status_code == 413 and "send fewer files" in res.json()["detail"]
        assert c.app.state.store.ids() == []
        assert upload(c, ("ok.png", encode(photo(), "PNG"))).status_code == 200


def test_chunked_body_over_the_cap_is_413(tmp_path):
    """Without a Content-Length the body is counted as it streams in."""
    import anyio

    app = create_app(Settings(tmp_dir=str(tmp_path / "s")))
    chunks = [{"type": "http.request", "body": b"[" + b" " * (1024 * 1024), "more_body": True}] * 3
    sent: list[dict] = []

    async def receive() -> dict:
        return chunks.pop() if chunks else {"type": "http.request", "body": b"]", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": "/api/jobs", "raw_path": b"/api/jobs", "query_string": b"",
             "root_path": "", "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
             "client": ("127.0.0.1", 1), "server": ("testserver", 80), "state": {}}
    anyio.run(app, scope, receive, send)
    assert sent[0]["status"] == 413 and b"too large" in sent[1]["body"]


# ------------------------------------------------------------------ admission of image decoding
def test_job_slots_bound_the_queue_and_memory():
    """Waiting is bounded (new requests get Busy), memory-planned work runs within the budget."""
    slots = api_module.JobSlots(max_jobs=2, memory_bytes=100, max_waiting=1)
    first = slots.acquire(60, timeout=1)
    assert first == 60
    got: list[int | None] = []
    waiter = threading.Thread(target=lambda: got.append(slots.acquire(60, timeout=5)))
    waiter.start()
    deadline = time.time() + 5
    while slots.stats()["waiting"] != 1 and time.time() < deadline:
        time.sleep(0.01)
    assert slots.stats() == {"running": 1, "waiting": 1, "planned_bytes": 60}  # 60 + 60 > 100: must wait
    with pytest.raises(api_module.Busy):
        slots.acquire(1, timeout=1)  # a new request finds the queue full
    assert slots.acquire(1, timeout=0.05, bounded=False) is None  # admitted work queues behind (FIFO) → timeout
    slots.release(first)
    waiter.join()
    assert got == [60]
    slots.release(60)
    assert slots.acquire(10 ** 9, timeout=1) == 100  # bigger than the whole budget: clamped, runs alone
    assert slots.acquire(1, timeout=0.05) is None
    slots.release(100)


def test_full_job_queue_is_503_with_retry_after(client):
    (f,) = upload_ok(client, ("a.png", encode(photo(), "PNG")))
    slots = api_module.JobSlots(max_jobs=1, memory_bytes=1 << 30, max_waiting=1)
    client.app.state.jobs = slots
    held = slots.acquire(0, timeout=1)
    waiter = threading.Thread(target=lambda: slots.release(slots.acquire(0, timeout=5, bounded=False)))
    waiter.start()
    deadline = time.time() + 5
    while slots.stats()["waiting"] != 1 and time.time() < deadline:
        time.sleep(0.01)
    res = client.get(f"/api/files/{f['id']}/preview", params={"max": 77})  # not cached: needs a decode
    assert res.status_code == 503 and res.headers["retry-after"] == "5"
    res = upload(client, ("b.png", encode(photo(seed=3), "PNG")))
    assert res.status_code == 503 and res.headers["retry-after"] == "5"
    assert client.get(f"/api/files/{f['id']}").status_code == 200  # file info never decodes
    assert client.delete(f"/api/files/{f['id']}").status_code == 204  # never queues behind jobs
    slots.release(held)
    waiter.join()


def test_a_multi_image_upload_is_never_cut_off_between_its_files(client, monkeypatch):
    """Only a request's first decode can be refused as busy: the rest of the batch waits instead."""
    calls: list[bool] = []
    real = api_module.JobSlots.acquire

    def spy(self, weight=0, *, timeout, bounded=True):
        calls.append(bounded)
        return real(self, weight, timeout=timeout, bounded=bounded)

    monkeypatch.setattr(api_module.JobSlots, "acquire", spy)
    upload_ok(client, *[(f"{i}.png", encode(photo(seed=i), "PNG")) for i in range(3)])
    assert calls == [True, False, False]
