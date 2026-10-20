"""app.clients.renderer against an in-process fake renderer on a Unix socket: wire format, error mapping (SSRF
refusals become clear messages, never URLs or engine output), size caps and cancellation. The ``services``
tests at the end use a real heypdf-renderer socket when ``HEYP_RENDERER_TEST_SOCKET`` is set; they skip otherwise."""
from __future__ import annotations

import base64
import json
import os
import socketserver
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler

import pytest

from app.clients import renderer
from app.core.errors import Busy, Cancelled, error_info

PDF = b"%PDF-1.7\n" + b"\x00" * 64 + b"\n%%EOF\n"


class FakeRenderer(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    scenario: dict = {}
    seen: list[dict] = []

    def log_message(self, *a):
        pass

    def address_string(self):
        return "unix"

    def _send(self, status, body: bytes, ctype="application/json", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            ok = self.scenario.get("health", True)
            return self._send(200 if ok else 503, json.dumps({"status": "ok" if ok else "starting"}).encode())
        self._send(404, b"{}")

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.seen.append(payload)
        sc = self.scenario
        if sc.get("sleep"):
            time.sleep(sc["sleep"])
        if "error" in sc:
            status, body, headers = sc["error"]
            return self._send(status, json.dumps(body).encode(), headers=headers)
        info = base64.b64encode(json.dumps({"final_url": "https://example.com/", "page_height": 1234,
                                            "truncated": False}).encode()).decode()
        self._send(200, sc.get("body", PDF), sc.get("ctype", "application/pdf"), {"X-Render-Info": info})


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@pytest.fixture
def fake(monkeypatch):
    FakeRenderer.scenario = {}
    FakeRenderer.seen = []
    d = tempfile.mkdtemp(prefix="r", dir="/tmp")
    path = os.path.join(d, "r.sock")
    server = _Server(path, FakeRenderer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("HEYP_RENDERER_SOCKET", path)
    yield FakeRenderer
    server.shutdown()
    server.server_close()
    os.unlink(path)
    os.rmdir(d)


def test_health(fake):
    assert renderer.configured() and renderer.health() == (True, "")
    fake.scenario["health"] = False
    assert renderer.health() == (False, renderer.UNREACHABLE)


def test_not_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("HEYP_RENDERER_SOCKET", str(tmp_path / "missing.sock"))
    assert renderer.health() == (False, renderer.NOT_CONFIGURED)
    regular = tmp_path / "file.sock"
    regular.write_text("not a socket")
    monkeypatch.setenv("HEYP_RENDERER_SOCKET", str(regular))
    assert not renderer.configured()


def test_render_ok(fake, tmp_path):
    res = renderer.render({"url": "https://example.com"}, dest=tmp_path / "o.pdf", deadline=time.monotonic() + 30)
    assert res.path.read_bytes() == PDF
    assert res.info["page_height"] == 1234 and fake.seen == [{"url": "https://example.com"}]


@pytest.mark.parametrize("ctype,body", [("text/html", b"<h1>hi</h1>"), ("image/png", b"\x89PNG\r\n\x1a\n")])
def test_anything_but_a_pdf_is_refused(fake, tmp_path, ctype, body):
    fake.scenario.update(ctype=ctype, body=body)
    with pytest.raises(renderer.RenderError) as e:
        renderer.render({"html": "x"}, dest=tmp_path / "o")
    assert e.value.status == 502 and e.value.code == "bad_response"


@pytest.mark.parametrize("status,body,expect_status,needle", [
    (422, {"error": "blocked_target", "detail": "private"}, 422, "private, local or reserved"),
    (422, {"error": "blocked_target", "detail": "link-local"}, 422, "private, local or reserved"),
    (422, {"error": "blocked_target", "detail": "dns-failure"}, 422, "could not be found"),
    (422, {"error": "blocked_target", "detail": "denied-cidr"}, 422, "belongs to this server"),
    (422, {"error": "blocked_target", "detail": "port-not-allowed"}, 422, "standard ports"),
    (400, {"error": "invalid_url", "detail": "only http and https URLs can be rendered"}, 422, "only http and https"),
    (422, {"error": "not_a_page", "detail": "the URL is a file download"}, 422, "file download"),
    (422, {"error": "too_many_navigations", "detail": "page kept navigating"}, 422, "kept redirecting"),
    (413, {"error": "too_large", "detail": "page exceeded the download budget"}, 413, "too large"),
    (504, {"error": "timeout", "detail": "page did not finish loading in time"}, 504, "too long"),
    (502, {"error": "navigation_failed", "detail": "net::ERR_NAME_NOT_RESOLVED"}, 502, "could not be found"),
    (502, {"error": "navigation_failed", "detail": "net::ERR_CERT_DATE_INVALID"}, 502, "certificate"),
    (502, {"error": "navigation_failed", "detail": "net::ERR_FOO_BAR"}, 502, "foo bar"),
    (400, {"error": "bad_request", "detail": "viewport_width: too small"}, 422, "options are not valid"),
    (500, {"detail": "Traceback ... /srv/renderer.py"}, 502, "renderer failed"),
])
def test_error_mapping(fake, tmp_path, status, body, expect_status, needle):
    fake.scenario["error"] = (status, body, {})
    with pytest.raises(renderer.RenderError) as e:
        renderer.render({"url": "https://secret.example/?token=abc"}, dest=tmp_path / "o")
    info = error_info(e.value)
    assert info["status"] == expect_status and needle in info["detail"], info
    assert "secret.example" not in info["detail"] and "Traceback" not in info["detail"]


def test_busy(fake, tmp_path):
    fake.scenario["error"] = (503, {"error": "busy", "detail": "renderer queue is full"}, {"Retry-After": "5"})
    with pytest.raises(Busy) as e:
        renderer.render({"html": "x"}, dest=tmp_path / "o")
    assert e.value.retry_after == 5


def test_result_size_cap(fake, tmp_path):
    fake.scenario["body"] = PDF + b"\x00" * 5000
    with pytest.raises(renderer.RenderError) as e:
        renderer.render({"html": "x"}, dest=tmp_path / "o", max_result_bytes=1000)
    assert e.value.status == 413 and not (tmp_path / "o").exists()


def test_cancel_is_prompt(fake, tmp_path):
    fake.scenario["sleep"] = 10
    t0 = time.monotonic()

    def check_cancel():
        if time.monotonic() - t0 > 0.4:
            raise Cancelled()

    with pytest.raises(Cancelled):
        renderer.render({"html": "x"}, dest=tmp_path / "o", check_cancel=check_cancel,
                        deadline=time.monotonic() + 60)
    assert time.monotonic() - t0 < 3


def test_deadline(fake, tmp_path):
    fake.scenario["sleep"] = 10
    with pytest.raises(renderer.RenderError) as e:
        renderer.render({"html": "x"}, dest=tmp_path / "o", deadline=time.monotonic() + 1)
    assert e.value.status == 504


def test_unreachable(monkeypatch, tmp_path):
    monkeypatch.setenv("HEYP_RENDERER_SOCKET", str(tmp_path / "gone.sock"))
    with pytest.raises(renderer.RenderError) as e:
        renderer.render({"html": "x"}, dest=tmp_path / "o")
    assert e.value.status == 503


# ------------------------------------------------------------------------------- real heypdf-renderer (services)
REAL = os.environ.get("HEYP_RENDERER_TEST_SOCKET", "")
services = pytest.mark.skipif(not REAL, reason="set HEYP_RENDERER_TEST_SOCKET to a running heypdf-renderer socket")


@pytest.fixture
def real(monkeypatch):
    monkeypatch.setenv("HEYP_RENDERER_SOCKET", REAL)


@pytest.mark.services
@services
def test_real_renders_inline_html(real, tmp_path):
    assert renderer.health() == (True, "")
    res = renderer.render({"html": "<body style='background:#0a0'><h1>heypdf</h1></body>", "viewport_width": 800},
                          dest=tmp_path / "o.pdf", deadline=time.monotonic() + 60)
    assert res.path.read_bytes().startswith(b"%PDF-")
    paged = renderer.render({"html": "<h1>pdf</h1>", "pdf": {"single_page": False, "format": "A4"}},
                            dest=tmp_path / "p.pdf", deadline=time.monotonic() + 60)
    assert paged.path.read_bytes().startswith(b"%PDF-")


@pytest.mark.services
@services
@pytest.mark.parametrize("url,status", [
    ("file:///etc/passwd", 422), ("http://127.0.0.1:8080/api/health", 422), ("http://169.254.169.254/latest/", 422),
    ("http://[::ffff:169.254.169.254]/", 422), ("http://localhost/", 422), ("http://10.0.0.1/", 422),
    ("http://docs:8080/healthz", 422), ("http://metadata.google.internal/", 422), ("http://0x7f.1/", 422),
])
def test_real_ssrf_targets_are_refused(real, tmp_path, url, status):
    """SSRF regression through the app's own client: internal targets never render (the pre-flight answers for
    literal/obvious ones; the egress proxy for names — e.g. ``docs`` resolves to a private address there)."""
    with pytest.raises(renderer.RenderError) as e:
        renderer.render({"url": url}, dest=tmp_path / "o", deadline=time.monotonic() + 60)
    assert e.value.status == status, (e.value.status, e.value.detail)
    host = url.split("//")[-1].split("/")[0]
    assert not host or host not in e.value.detail
