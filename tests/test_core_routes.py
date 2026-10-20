"""HTTP surface (PLAN.md §2–§3): uploads of every kind, downloads, /api/config, /api/tools, legal pages, the
single-page app at "/", the scan pages' camera permission, and the hosted-mode policies."""
from __future__ import annotations

import io
import json
import logging
import time
import zipfile

import pymupdf
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import main as main_module
from app.config import Settings, VERSION
from app.core import registry
from app.main import CSP, create_app
from app.routers import legal as legal_routes

HOSTED = dict(mode="hosted", operator_name="Ada Operator", operator_email="ops@example.org",
              public_url="https://heypdf.example.org")


def png(size=(64, 48), color=(200, 40, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def pdf(pages: int = 2, password: str | None = None) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    kw = {"encryption": pymupdf.PDF_ENCRYPT_AES_256, "user_pw": password, "owner_pw": "o"} if password else {}
    data = doc.tobytes(**kw)
    doc.close()
    return data


def docx(pages: int = 4, bomb: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w:document/>")
        zf.writestr("docProps/app.xml", f"<Properties><Pages>{pages}</Pages></Properties>")
        if bomb:
            zf.writestr("word/media/x.bin", b"\0" * (20 << 20))
    return buf.getvalue()


@pytest.fixture
def make_client(tmp_path):
    clients = []

    def make(**kw) -> TestClient:
        settings = Settings(**{"tmp_dir": str(tmp_path / f"heypdf{len(clients)}"), "max_upload_mb": 30,
                               "max_files": 10, **kw})
        c = TestClient(create_app(settings))
        c.__enter__()
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.__exit__(None, None, None)
    main_module.set_access_log(False)


@pytest.fixture
def client(make_client):
    return make_client()


def upload(client, *files):
    return client.post("/api/files", files=[("files", (name, data, "application/octet-stream")) for name, data in files])


def upload_one(client, name: str, data: bytes) -> dict:
    res = upload(client, (name, data))
    assert res.status_code == 200, res.text
    assert res.json()["errors"] == [], res.text
    return res.json()["files"][0]


# ------------------------------------------------------------------ uploads of every kind
def test_image_fileinfo_gains_the_v2_fields(client):
    f = upload_one(client, "a.png", png())
    assert f["kind"] == "image" and f["source"] == "upload" and f["parent_ids"] == [] and f["tool"] is None
    assert f["mime"] == "image/png" and f["width"] == 64 and f["encrypted"] is False and f["pages"] is None
    assert client.get(f"/api/files/{f['id']}").json() == f


def test_pdf_uploads_are_probed_not_decoded(client):
    f = upload_one(client, "report.pdf", pdf(3))
    assert {k: f[k] for k in ("kind", "format", "mime", "pages", "encrypted")} == \
        {"kind": "pdf", "format": "pdf", "mime": "application/pdf", "pages": 3, "encrypted": False}
    assert client.get(f"/api/files/{f['id']}").json() == f
    locked = upload_one(client, "locked.pdf", pdf(1, password="pw"))
    assert locked["encrypted"] is True and locked["pages"] is None
    # the image preview refuses documents clearly (pages are rendered by /api/pdf/{id}/pages/{n}.png)
    res = client.get(f"/api/files/{f['id']}/preview")
    assert res.status_code == 415 and "report.pdf is not an image" in res.json()["detail"]


def test_office_text_html_and_certificate_uploads(client):
    res = upload(client, ("letter.docx", docx(4)), ("notes.txt", b"plain text\n"), ("page.html", b"<!doctype html><p>hi"),
                 ("id.p12", b"\x30\x82\x01\x00" + b"\x02" * 300))
    assert res.status_code == 200 and res.json()["errors"] == []
    kinds = [(f["kind"], f["format"]) for f in res.json()["files"]]
    assert kinds == [("office", "docx"), ("office", "txt"), ("html", "html"), ("cert", "p12")]
    assert res.json()["files"][0]["pages"] == 4


def test_refused_uploads_and_their_statuses(client):
    res = upload(client, ("bomb.docx", docx(bomb=True)), ("x.exe", b"MZ\x90\x00" + b"\x00" * 64))
    assert res.status_code == 415  # 415 ranks above 422 (PLAN.md §3 upload status priority)
    errors = {e["name"]: e["detail"] for e in res.json()["errors"]}
    assert "zip bomb" in errors["bomb.docx"] and "“.exe” files can't be opened" in errors["x.exe"]
    assert client.app.state.store.ids() == []


def test_hosted_upload_byte_budget_is_429(make_client):
    c = make_client(**HOSTED, limit_upload_mb_hour=1)
    assert upload(c, ("a.bin", png((500, 500)))).status_code == 200
    big = b"%PDF-1.4\n" + bytes(1100 * 1024)
    res = upload(c, ("b.pdf", big))
    assert res.status_code == 429 and int(res.headers["retry-after"]) >= 1
    assert "per hour" in res.json()["detail"]


# ------------------------------------------------------------------ downloads
def test_download_streams_the_bytes_as_an_attachment(client):
    data = pdf(2)
    f = upload_one(client, "Bericht ü.pdf", data)
    res = client.get(f"/api/files/{f['id']}/download")
    assert res.status_code == 200 and res.content == data
    assert res.headers["content-type"] == "application/pdf"
    assert res.headers["content-disposition"].startswith("attachment;")
    assert "filename*=UTF-8''Bericht%20%C3%BC.pdf" in res.headers["content-disposition"]
    assert res.headers["content-security-policy"] == CSP + "; sandbox"
    assert client.get("/api/files/" + "0" * 32 + "/download").status_code == 404


def test_html_downloads_are_sandboxed_and_certificates_never_served(client):
    page = upload_one(client, "evil.html", b"<!doctype html><script>alert(1)</script>")
    res = client.get(f"/api/files/{page['id']}/download")
    assert res.status_code == 200 and "sandbox" in res.headers["content-security-policy"]
    assert res.headers["content-disposition"].startswith("attachment;")
    cert = upload_one(client, "id.pfx", b"\x30\x82\x01\x00" + b"\x02" * 300)
    assert client.get(f"/api/files/{cert['id']}/download").status_code == 403


# ------------------------------------------------------------------ /api/config and /api/health
def test_config_local_is_truthful(client):
    body = client.get("/api/config").json()
    assert body["mode"] == "local" and body["version"] == VERSION
    assert body["sibling_url"] == "http://localhost:8080"  # heyp, for the "heyp · heypdf" switch
    assert body["ttl_minutes"] == 60 and body["hard_ttl_minutes"] == 0
    assert set(body["features"]) == {"docs", "renderer", "ai", "heic", "avif"}
    assert body["ai"]["provider"] == "Anthropic" and isinstance(body["ai"]["enabled"], bool)
    assert body["limits"]["rate_limits"] is False and body["limits"]["max_upload_mb"] == 30
    assert body["legal"] == {"privacy": "/privacy", "terms": "/terms", "credits": "/credits"}
    assert body["source_url"] == "https://github.com/uroy80/heypdf"


BANNED_COPY = ("Private by design", "Local only", "this machine", "edition", "never leave", "never the internet")


@pytest.mark.parametrize("ttl,hard", [(60, -1), (90, 240), (30, 1440)])
def test_config_hosted_states_both_deadlines_and_no_slogans(make_client, ttl, hard):
    c = make_client(**HOSTED, ttl_minutes=ttl, hard_ttl_minutes=hard, sibling_url="https://heyp.example.org")
    body = c.get("/api/config").json()
    assert body["mode"] == "hosted" and body["ttl_minutes"] == ttl
    assert body["hard_ttl_minutes"] == (240 if hard == -1 else hard)
    assert body["public_url"] == "https://heypdf.example.org" and body["sibling_url"] == "https://heyp.example.org"
    assert body["operator"] == {"name": "Ada Operator", "email": "ops@example.org"}
    assert body["limits"]["rate_limits"] is True and body["limits"]["jobs_per_minute"] == 30
    text = json.dumps(body).lower()
    assert not any(b.lower() in text for b in BANNED_COPY)


def test_request_cap_is_published_only_when_set(make_client):
    """The UI sizes upload requests from limits.max_request_mb when the server publishes it (behind a CDN),
    and keeps its own CDN-safe default otherwise; the server enforces the same cap before parsing."""
    plain = make_client()
    assert "max_request_mb" not in plain.get("/api/config").json()["limits"]
    assert "max_request_mb" not in plain.get("/api/health").json()["limits"]
    capped = make_client(max_request_mb=31)
    assert capped.get("/api/config").json()["limits"]["max_request_mb"] == 31
    assert capped.get("/api/health").json()["limits"]["max_request_mb"] == 31
    r = capped.post("/api/files", content=b"x", headers={"content-type": "multipart/form-data; boundary=b",
                                                          "content-length": str(31 * 2**20 + 1)})
    assert r.status_code == 413 and "31 MB" in r.json()["detail"]


def test_health_lists_the_engines(client):
    features = client.get("/api/health").json()["features"]
    assert features["pdf"] is True and features["docs"] is False and features["renderer"] is False


# ------------------------------------------------------------------ /api/tools
def test_tools_listing(client):
    tools = client.get("/api/tools").json()["tools"]
    assert len(tools) == len(registry.all_tools()) == 31  # 29 PDF tools + Summarize and Translate
    cats = [t["category"] for t in tools]
    assert cats == sorted(cats, key=list(registry.CATEGORIES).index)  # contents-page order
    merge = next(t for t in tools if t["id"] == "pdf.merge")
    assert merge["slug"] == "merge" and merge["available"] is True and "app" not in merge
    assert client.get("/api/tools/pdf.merge").json()["id"] == "pdf.merge"
    assert client.get("/api/tools/pdf.missing").status_code == 404
    assert client.post("/api/tools/pdf.merge/preview", json={}).status_code == 405  # no live tool previews


# ------------------------------------------------------------------ legal pages
@pytest.mark.parametrize("page", ["privacy", "terms", "credits"])
def test_legal_pages_render(client, page):
    res = client.get(f"/{page}")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/html")
    assert legal_routes.PAGES[page] in res.text
    assert res.headers["content-security-policy"] == CSP
    assert client.head(f"/{page}").status_code == 200


def test_legal_page_falls_back_when_the_renderer_fails(make_client, monkeypatch):
    from app import legal

    def broken(*_a, **_k):
        raise RuntimeError("template missing")

    monkeypatch.setattr(legal, "render", broken)
    legal_routes._cache.clear()
    c = make_client(**HOSTED)
    res = c.get("/privacy")
    assert res.status_code == 200 and "Ada Operator" in res.text and "ops@example.org" in res.text
    assert "https://github.com/uroy80/heypdf" in res.text
    legal_routes._cache.clear()


def test_hosted_privacy_page_names_the_operator(make_client):
    legal_routes._cache.clear()
    res = make_client(**HOSTED).get("/privacy")
    assert res.status_code == 200 and "Ada Operator" in res.text


# ------------------------------------------------------------------ the single-page app at "/"
def test_spa_fallback(client):
    for path in ("/", "/merge", "/compress/", "/scan", "/phone", "/phone/abcdefghijklmnop"):
        res = client.get(path)
        assert res.status_code == 200 and "<title>heypdf :)" in res.text, path
        assert res.headers["cache-control"] == "no-cache"
    assert client.get("/js/main.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/js/missing.js").status_code == 404  # never index.html for assets
    for path in ("/api/nope", "/api/files/x/y/z", "/docs", "/redoc", "/openapi.json"):
        res = client.get(path)
        assert res.status_code == 404 and res.headers["content-type"] == "application/json", path
    assert client.get("/.env").status_code == 404


def test_fonts_are_served_with_font_types(client):
    """The slim image has no /etc/mime.types: fonts must not go out as text/plain."""
    from pathlib import Path

    woff2 = next(Path(main_module.STATIC_DIR, "fonts").glob("*.woff2"))
    res = client.get(f"/fonts/{woff2.name}")
    assert res.status_code == 200 and res.headers["content-type"] == "font/woff2"
    for font in ("anton", "dancing_script", "great_vibes", "caveat", "homemade_apple"):  # the UI's previews
        res = client.get(f"/api/assets/fonts/{font}.ttf")
        assert res.status_code == 200 and res.headers["content-type"] == "font/ttf", font
    assert client.get("/api/assets/fonts/helvetica.ttf").status_code == 404  # a PDF base font has no file
    assert client.get("/api/assets/fonts/nope.ttf").status_code == 404


def test_camera_is_allowed_on_the_scan_pages_only(client):
    for path in ("/scan", "/phone", "/phone/abcdefghijklmnop"):
        assert client.get(path).headers["permissions-policy"].startswith("camera=(self)"), path
    for path in ("/", "/merge", "/api/health", "/scanner"):
        assert "camera=()" in client.get(path).headers["permissions-policy"], path


def test_scan_qr_link_points_at_the_phone_page(make_client):
    hosted = make_client(**HOSTED).post("/api/scan/sessions")
    assert hosted.status_code == 201 and hosted.json()["url"].startswith("https://heypdf.example.org/phone#")
    local = make_client().post("/api/scan/sessions")
    assert local.json()["url"].startswith("http://testserver/phone#")  # the address the computer used


# ------------------------------------------------------------------ hosted-mode policies
def test_hosted_mode_answers_api_requests_with_no_store(make_client):
    c = make_client(**HOSTED)
    f = upload_one(c, "a.png", png())
    for path in (f"/api/files/{f['id']}", f"/api/files/{f['id']}/preview", f"/api/files/{f['id']}/download",
                 "/api/health", "/api/config"):
        assert c.get(path).headers["cache-control"] == "no-store", path
    assert c.get("/").headers["cache-control"] == "no-cache"


def test_local_mode_lets_the_browser_cache_previews(client):
    f = upload_one(client, "a.png", png())
    assert client.get(f"/api/files/{f['id']}/preview").headers["cache-control"] == "private, max-age=3600"


def test_hosted_mode_keeps_no_access_log(make_client):
    flt = next(f for f in logging.getLogger("uvicorn.access").filters
               if isinstance(f, main_module.HealthCheckLogFilter))
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("203.0.113.9:5000", "GET", "/api/files", "1.1", 200), None)
    make_client()
    assert flt.filter(record)
    make_client(**HOSTED)
    assert not flt.filter(record)


def test_hard_ttl_applies_in_hosted_mode(make_client):
    c = make_client(**HOSTED)
    f = upload_one(c, "a.png", png())
    assert f["expires_at"] is not None and 0 < f["expires_at"] - time.time() <= 240 * 60
    meta = c.app.state.store.root / f["id"] / "meta.json"
    data = json.loads(meta.read_text())
    data["hard_deadline"] = time.time() - 1
    meta.write_text(json.dumps(data))
    assert c.get(f"/api/files/{f['id']}").status_code == 404  # gone at its hard deadline, however active
