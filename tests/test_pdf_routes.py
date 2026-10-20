"""heypdf viewer routes (/api/pdf/{id}/info, pages/{n}.png, words, search) and the tool registrations."""
from __future__ import annotations

import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.core import registry
from app.pdf.routes import pdf_router
from app.store import FileStore
from tests.fixtures.pdf.factory import pdfs  # noqa: F401  (session fixture)

PDF_CORE_TOOLS = {
    "pdf.merge", "pdf.split", "pdf.remove_pages", "pdf.extract_pages", "pdf.organize", "pdf.scan",
    "pdf.from_images", "pdf.to_images", "pdf.to_word", "pdf.to_excel", "pdf.to_markdown",
    "pdf.rotate", "pdf.page_numbers", "pdf.watermark", "pdf.crop", "pdf.edit", "pdf.forms",
    "pdf.unlock", "pdf.protect", "pdf.sign", "pdf.redact", "pdf.compare",
}


def _app(tmp_path, mode: str = "local"):
    kw = {"tmp_dir": str(tmp_path / "store"), "mode": mode}
    if mode == "hosted":
        kw.update(public_url="https://heyp.example.org", operator_name="Op", operator_email="op@example.org")
    try:
        settings = Settings(**kw)
    except TypeError:
        settings = Settings(tmp_dir=kw["tmp_dir"])
    store = FileStore(settings.tmp_dir, 3600)
    store.init()
    app = FastAPI()
    app.state.settings, app.state.store = settings, store
    app.include_router(pdf_router, prefix="/api")
    return app, store


def _put(store: FileStore, path, kind: str = "pdf") -> str:
    with open(path, "rb") as fh:
        stored = store.save(path.name, fh, max_bytes=50 * 1024 * 1024)
    store.set_info(stored.id, kind=kind)
    return stored.id


@pytest.fixture()
def env(tmp_path):
    app, store = _app(tmp_path)
    with TestClient(app) as client:
        yield client, store


def test_info(env, pdfs):
    client, store = env
    fid = _put(store, pdfs.geometry)
    r = client.get(f"/api/pdf/{fid}/info")
    assert r.status_code == 200
    body = r.json()
    assert body["pages"] == 4 and body["rotations"] == [0, 90, 180, 270]
    assert body["page_sizes"][0] == [550.0, 750.0] and body["page_sizes"][1] == [750.0, 550.0]
    assert body["has_text"] and not body["encrypted"]
    assert r.headers["cache-control"].startswith("private")
    assert store.read_aux(fid, "pdf-info.json") is not None  # cached next to the upload
    form = client.get(f"/api/pdf/{_put(store, pdfs.acroform)}/info").json()
    assert {f["name"] for f in form["fields"]} >= {"full_name", "plan", "country"}
    assert form["has_form"]
    report = client.get(f"/api/pdf/{_put(store, pdfs.report)}/info").json()
    assert report["outline"][0]["title"].startswith("Chapter 1")


def test_info_of_encrypted_pdf(env, pdfs):
    client, store = env
    fid = _put(store, pdfs.encrypted)
    body = client.get(f"/api/pdf/{fid}/info").json()
    assert body["needs_password"] is True and body["pages"] is None
    unlocked = client.get(f"/api/pdf/{fid}/info", headers={"X-PDF-Password": "userpw"}).json()
    assert unlocked["pages"] == 6
    r = client.get(f"/api/pdf/{fid}/pages/1.png")
    assert r.status_code == 422 and r.json()["needs_password"] is True


def test_page_render_respects_rotation_and_max(env, pdfs):
    client, store = env
    fid = _put(store, pdfs.geometry)
    r = client.get(f"/api/pdf/{fid}/pages/2.png?max=300")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(r.content))
    assert img.size[0] == 360 and img.size[0] > img.size[1]  # bucketed up to 360 px, landscape page
    assert store.read_aux(fid, "page-2-360.png") == r.content
    assert client.get(f"/api/pdf/{fid}/pages/9.png").status_code == 404
    assert client.get(f"/api/pdf/{fid}/pages/1.png?max=99999").status_code == 422


def test_words_are_visible_page_fractions(env, pdfs):
    client, store = env
    fid = _put(store, pdfs.geometry)
    body = client.get(f"/api/pdf/{fid}/pages/2/words").json()
    marker = next(w for w in body["words"] if w["t"] == "marker")
    assert 0 <= marker["x"] <= 1 and marker["h"] > marker["w"]  # vertical on the rotated page
    assert body["width"] == 750.0


def test_search_route(env, pdfs):
    client, store = env
    fid = _put(store, pdfs.report)
    r = client.post(f"/api/pdf/{fid}/search", json={"presets": ["email"], "terms": ["Bengaluru"]})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 6
    kinds = {m["kind"] for m in body["matches"]}
    assert kinds == {"email", "term"} and body["matches"][0]["boxes"]
    assert client.post(f"/api/pdf/{fid}/search", json={}).status_code == 422
    assert client.post(f"/api/pdf/{fid}/search", json={"regex": ["("]}).status_code == 422


def test_route_abuse(env, pdfs):
    client, store = env
    assert client.get("/api/pdf/../../etc/passwd/info").status_code == 404
    assert client.get("/api/pdf/" + "0" * 32 + "/info").status_code == 404
    img = _put(store, pdfs.jpeg, kind="image")
    assert client.get(f"/api/pdf/{img}/info").status_code == 415
    ps = _put(store, pdfs.not_pdf)
    assert client.get(f"/api/pdf/{ps}/pages/1.png").status_code == 415
    broken = _put(store, pdfs.broken)
    assert client.get(f"/api/pdf/{broken}/pages/1.png").status_code in (200, 422)


def test_hosted_mode_is_no_store(tmp_path, pdfs):
    app, store = _app(tmp_path, "hosted")
    if not getattr(app.state.settings, "hosted", False):
        pytest.skip("Settings could not be built in hosted mode here")
    with TestClient(app) as client:
        fid = _put(store, pdfs.report)
        assert client.get(f"/api/pdf/{fid}/info").headers["cache-control"] == "no-store"
        assert client.get(f"/api/pdf/{fid}/pages/1.png").headers["cache-control"] == "no-store"


# ------------------------------------------------------------------ viewer abuse limits
def test_viewer_work_is_charged_to_the_client_in_hosted_mode(tmp_path, pdfs):
    from app.core.limits import Limiter, Rule
    from app.pdf import routes

    app, store = _app(tmp_path, "hosted")
    if not getattr(app.state.settings, "limits_enabled", False):
        pytest.skip("Settings could not be built in hosted mode here")
    app.state.limiter = limiter = Limiter(app.state.settings)
    limiter.rules["viewer"] = Rule(30, 3600.0, "Too many pages viewed at once")  # ~no refill in the test
    with TestClient(app) as client:
        fid = _put(store, pdfs.report)
        assert routes.render_cost(480) == 1 and routes.render_cost(1600) == 12 and routes.render_cost(2400) == 25
        assert client.get(f"/api/pdf/{fid}/pages/1.png?max=2400").status_code == 200   # 25 of 30 tokens
        assert store.read_aux(fid, "page-1-2400.png") is None                          # zoom renders aren't kept
        r = client.get(f"/api/pdf/{fid}/pages/2.png?max=2400")
        assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
        assert client.get(f"/api/pdf/{fid}/pages/1.png?max=480").status_code == 200   # 1 token (miss)
        # a cached page costs nothing
        assert all(client.get(f"/api/pdf/{fid}/pages/1.png?max=480").status_code == 200 for _ in range(10))
        assert client.post(f"/api/pdf/{fid}/search", json={"terms": ["x"]}).status_code == 200  # 3 tokens
        assert client.get(f"/api/pdf/{fid}/pages/2/words").status_code == 200                 # the last one
        r = client.get(f"/api/pdf/{fid}/pages/2/words")
        assert r.status_code == 429 and "retry-after" in r.headers
    # the default bucket exists without any configuration
    limiter2 = Limiter(app.state.settings)
    routes._viewer_rule(limiter2, app.state.settings)
    assert limiter2.rules["viewer"].capacity == routes.VIEWER_PER_MINUTE


def test_render_cache_is_bounded_per_file_and_by_free_space(env, pdfs, monkeypatch):
    from app.pdf import routes

    client, store = env
    fid = _put(store, pdfs.report)
    first = client.get(f"/api/pdf/{fid}/pages/2.png?max=480").content  # text pages of similar size
    monkeypatch.setattr(routes, "CACHE_MAX_BYTES_PER_FILE", int(len(first) * 2.5))
    for n in range(3, 7):
        assert client.get(f"/api/pdf/{fid}/pages/{n}.png?max=480").status_code == 200
    kept = sorted(p.name for p in (store.root / fid).iterdir() if p.name.startswith("page-"))
    total = sum((store.root / fid / name).stat().st_size for name in kept)
    assert total <= routes.CACHE_MAX_BYTES_PER_FILE and "page-6-480.png" in kept and "page-2-480.png" not in kept
    monkeypatch.setattr(routes, "_store_has_room", lambda _store: False)  # store filling up: serve, don't keep
    assert client.get(f"/api/pdf/{fid}/pages/5.png?max=240").status_code == 200
    assert store.read_aux(fid, "page-5-240.png") is None


def test_viewer_admission_is_bounded(env, pdfs, monkeypatch):
    import threading

    from app.pdf import routes

    client, store = env
    fid = _put(store, pdfs.report)
    full = threading.BoundedSemaphore(1)
    full.acquire()
    monkeypatch.setattr(routes, "_admission_semaphore", lambda _request: full)
    monkeypatch.setattr(routes, "ADMISSION_WAIT_S", 0.05)
    r = client.get(f"/api/pdf/{fid}/pages/3.png?max=480")
    assert r.status_code == 503 and r.headers["retry-after"] == "2"
    assert client.get(f"/api/pdf/{fid}/pages/3/words").status_code == 503
    full.release()
    assert client.get(f"/api/pdf/{fid}/pages/3.png?max=480").status_code == 200


# ------------------------------------------------------------------ registry
def test_every_pdf_core_tool_is_registered():
    import app.tools.pdf_convert  # noqa: F401
    import app.tools.pdf_edit
    import app.tools.pdf_organize
    import app.tools.pdf_security  # noqa: F401

    ids = {t.id for t in registry.all_tools()}
    assert PDF_CORE_TOOLS <= ids
    for tid in PDF_CORE_TOOLS:
        tool = registry.get(tid)
        schema = registry.options_schema(tool)
        assert schema["type"] == "object" and schema.get("additionalProperties") is False, tid
        assert tool.category in registry.CATEGORIES and tool.outputs, tid
    assert app.tools.pdf_edit.router.prefix == "/pdf"
    assert app.tools.pdf_organize.router.prefix == "/scan"
    assert registry.get("pdf.unlock").accepts_encrypted
    assert registry.get("pdf.sign").inputs == ("pdf", "p12") or "cert" in registry.get("pdf.sign").inputs


def test_options_reject_unknown_fields():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        registry.get("pdf.rotate").options.model_validate({"angle": 90, "evil": 1})
