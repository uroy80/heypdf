"""Service hardening (PLAN.md §9): cross-origin guard, docs gating, dotfiles, config."""
from __future__ import annotations

from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from starlette.datastructures import Headers

import app.main as main_module
from app.config import Settings
from app.main import CROSS_ORIGIN_DETAIL, CSP, create_app, is_cross_origin


def png() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, "PNG")
    return buf.getvalue()


def make_client(tmp_path, **overrides) -> TestClient:
    settings = Settings(tmp_dir=str(tmp_path / "heypdf"), max_jobs=2, **overrides)
    return TestClient(create_app(settings))


@pytest.fixture()
def client(tmp_path):
    with make_client(tmp_path) as c:
        yield c


def upload(client: TestClient, headers: dict | None = None):
    return client.post("/api/files", files=[("files", ("a.png", png(), "image/png"))], headers=headers or {})


# ------------------------------------------------------------------ cross-origin guard
@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"},
    {"Origin": "http://testserver:8080"},          # other port = other origin
    {"Origin": "null"},                            # sandboxed iframe / file://
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "cross-site", "Origin": "http://testserver"},
])
def test_cross_origin_upload_is_403(client, headers):
    res = upload(client, headers)
    assert res.status_code == 403
    assert res.json() == {"detail": CROSS_ORIGIN_DETAIL}
    assert res.headers["content-security-policy"] == CSP
    assert client.app.state.store.ids() == []


@pytest.mark.parametrize("headers", [
    {},                                                       # curl / scripts
    {"Origin": "http://testserver"},
    {"Origin": "http://TestServer:80"},                       # default port, case-insensitive
    {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    {"Sec-Fetch-Site": "none"},
    {"Origin": "https://photos.example", "X-Forwarded-Host": "photos.example"},  # reverse proxy
])
def test_same_origin_and_non_browser_uploads_pass(client, headers):
    assert upload(client, headers).status_code == 200


def test_cross_origin_guard_covers_all_state_changing_methods(client):
    (f,) = upload(client).json()["files"]
    evil = {"Origin": "https://evil.example"}
    assert client.delete(f"/api/files/{f['id']}", headers=evil).status_code == 403
    assert client.post("/api/jobs", json={"tool": "pdf.rotate", "file_ids": [f["id"]]}, headers=evil).status_code == 403
    assert client.post("/api/scan/sessions", headers=evil).status_code == 403
    assert client.post(f"/api/pdf/{f['id']}/search", json={"query": "x"}, headers=evil).status_code == 403
    assert client.put("/api/files", headers=evil).status_code == 403
    assert client.get(f"/api/files/{f['id']}", headers=evil).status_code == 200  # reads are not blocked
    assert client.get("/api/health", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200
    assert client.delete(f"/api/files/{f['id']}").status_code == 204


def test_allow_cross_origin_disables_guard(tmp_path):
    with make_client(tmp_path, allow_cross_origin=True) as c:
        assert upload(c, {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_is_cross_origin_ipv6_host():
    assert not is_cross_origin(Headers({"origin": "http://[::1]:8080", "host": "[::1]:8080"}))
    assert is_cross_origin(Headers({"origin": "http://[::1]:9090", "host": "[::1]:8080"}))
    assert not is_cross_origin(Headers({"origin": "https://h.example:443", "host": "h.example"}))
    assert is_cross_origin(Headers({"origin": "ftp://h.example", "host": "h.example"}))


# ------------------------------------------------------------------ docs gating
@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_docs_are_off_by_default(client, path):
    assert client.get(path).status_code == 404


def test_docs_enabled_with_flag(tmp_path):
    with make_client(tmp_path, docs=True) as c:
        res = c.get("/docs")
        assert res.status_code == 200 and "content-security-policy" not in res.headers
        assert c.get("/openapi.json").status_code == 200
        assert c.get("/openapi.json").headers["content-security-policy"] == CSP
        assert c.get("/redoc").status_code == 200
        assert c.get("/").headers["content-security-policy"] == CSP


# ------------------------------------------------------------------ dotfiles
def test_dotfiles_are_never_served(tmp_path, monkeypatch):
    static = tmp_path / "static"
    (static / ".git").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>heypdf</title>")
    (static / ".DS_Store").write_bytes(b"Bud1")
    (static / ".env").write_text("SECRET=1")
    (static / ".git" / "config").write_text("[core]")
    monkeypatch.setattr(main_module, "STATIC_DIR", static)
    with make_client(tmp_path) as c:
        assert c.get("/").status_code == 200
        for path in ("/.DS_Store", "/.env", "/.git/config", "/js/../.env"):
            res = c.get(path)
            assert res.status_code == 404 and b"Bud1" not in res.content and b"SECRET" not in res.content
        assert c.head("/.DS_Store").status_code == 404


def test_real_static_dir_has_no_served_dotfiles(client):
    assert client.get("/.DS_Store").status_code == 404
    assert client.get("/js/main.js").status_code == 200


# ------------------------------------------------------------------ config
def test_settings_from_env_v11_fields():
    s = Settings.from_env({"HEYP_CACHE_MB": "64", "HEYP_ALLOW_CROSS_ORIGIN": "1", "HEYP_DOCS": "true",
                           "HEYP_JOB_MEMORY_MB": "3000"})
    assert (s.cache_mb, s.cache_bytes, s.allow_cross_origin, s.docs) == (64, 64 << 20, True, True)
    assert s.job_memory_bytes == 3000 << 20
    d = Settings.from_env({})
    assert (d.cache_mb, d.allow_cross_origin, d.docs, d.job_memory_mb) == (512, False, False, 0)
    assert d.max_pixels == 50_000_000  # 4 B/px in Pillow → 200 MB per copy at the limit
    assert 2 <= d.max_jobs <= 4
    assert Settings.from_env({"HEYP_DOCS": "0", "HEYP_CACHE_MB": "0"}).docs is False
    with pytest.raises(ValueError, match="HEYP_DOCS"):
        Settings.from_env({"HEYP_DOCS": "maybe"})
    with pytest.raises(ValueError, match="HEYP_CACHE_MB"):
        Settings.from_env({"HEYP_CACHE_MB": "-1"})


def test_app_store_uses_cache_budget(tmp_path):
    app = create_app(Settings(tmp_dir=str(tmp_path / "heypdf"), cache_mb=7))
    assert app.state.store.cache_bytes == 7 << 20


# ------------------------------------------------------------------ memory / request limits
def test_job_memory_budget_follows_the_container_limit(monkeypatch):
    import app.config as config

    monkeypatch.setattr(config, "memory_limit_bytes", lambda: 4 << 30)
    assert Settings(cache_mb=512).job_memory_bytes == int((4 << 30) * 0.6) - (512 << 20)
    monkeypatch.setattr(config, "memory_limit_bytes", lambda: 1 << 30)
    assert Settings().job_memory_bytes == 1 << 30  # never below 1 GiB
    monkeypatch.setattr(config, "memory_limit_bytes", lambda: None)
    assert Settings(cache_mb=0).job_memory_bytes == int((4 << 30) * 0.6)


def test_memory_limit_is_read_from_the_cgroup():
    from app.config import memory_limit_bytes

    value = memory_limit_bytes()
    assert value is None or value > 64 << 20


def test_request_cap_always_fits_one_full_size_file():
    assert Settings(max_upload_mb=100).max_request_bytes == 257 << 20
    assert Settings(max_upload_mb=900).max_request_bytes == 901 << 20


def test_job_slots_come_from_settings(tmp_path):
    app = create_app(Settings(tmp_dir=str(tmp_path / "heypdf"), max_jobs=3, job_memory_mb=1500))
    slots = app.state.jobs
    assert (slots.max_jobs, slots.memory_bytes, slots.max_waiting) == (3, 1500 << 20, 21)
    assert create_app(Settings(tmp_dir=str(tmp_path / "x"), max_jobs=30)).state.jobs.max_waiting == 8


def test_healthcheck_requests_are_not_access_logged():
    """The container HEALTHCHECK (every 30 s) must not flood the access log."""
    import logging

    flt = main_module.HealthCheckLogFilter()

    def record(path: str) -> logging.LogRecord:
        return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                                 ("127.0.0.1:5000", "GET", path, "1.1", 200), None)

    assert not flt.filter(record("/api/health"))
    assert not flt.filter(record("/api/health?x=1"))
    assert flt.filter(record("/api/files"))
    assert flt.filter(record("/api/healthz"))
    assert any(isinstance(f, main_module.HealthCheckLogFilter) for f in logging.getLogger("uvicorn.access").filters)
