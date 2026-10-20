"""pdf.from_html (app/tools/render.py) with the renderer client mocked: registration, URL
checks, what is sent to the renderer, render limits, outputs, warnings and errors — plus repository checks that
keep the service images honest (shared guard.py, pinned Playwright, seccomp profile, compose fragment)."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.clients import renderer
from app.core import registry
from app.core.errors import ToolError, error_info
from app.tools import render
from app.tools.render import HtmlToPdfOptions, normalize_url
from tests.test_tools_docs import FakeContext, FakeInput

ROOT = Path(__file__).resolve().parents[1]
PDF = b"%PDF-1.7\n%%EOF\n"


class FakeRender:
    def __init__(self, body=PDF, info=None, error=None):
        self.calls, self.body, self.info, self.error = [], body, info or {}, error

    def __call__(self, payload, *, dest, deadline=None, check_cancel=None, on_tick=None, **_):
        self.calls.append({"payload": payload, "deadline": deadline})
        if check_cancel:
            check_cancel()
        if on_tick:
            on_tick(2.0)
        if self.error:
            raise self.error
        Path(dest).write_bytes(self.body)
        return renderer.RenderResult(Path(dest), dict(self.info))


@pytest.fixture
def fake_render(monkeypatch):
    def install(**kw) -> FakeRender:
        f = FakeRender(**kw)
        monkeypatch.setattr(renderer, "render", f)
        return f
    return install


@pytest.fixture
def ctx(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    return FakeContext([], work)


def html_ctx(tmp_path, html: bytes, name="page.html") -> FakeContext:
    p = tmp_path / "in.html"
    p.write_bytes(html)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return FakeContext([FakeInput("f" * 32, name, p, "html", "text/html", {"format": "html"})], work)


# ------------------------------------------------------------------------------------------ registration
def test_registered_per_plan():
    t = registry.get("pdf.from_html")
    assert (t.category, t.engine, t.lane, t.inputs, t.max_files) == ("Convert to PDF", "renderer", "render",
                                                                     ("html",), 1)
    assert t.min_files == 0  # a web address alone is enough
    assert registry.options_schema(t).get("additionalProperties") is False


def test_renderer_engine_check(monkeypatch, tmp_path):
    monkeypatch.setenv("HEYP_RENDERER_SOCKET", str(tmp_path / "none.sock"))
    registry.register_engine_check("renderer", renderer.health)
    ok, reason = registry.engine_status("renderer", SimpleNamespace(ai_key_set=False))
    assert not ok and reason == renderer.NOT_CONFIGURED


@pytest.mark.parametrize("raw,url,host", [
    ("example.com", "https://example.com", "example.com"), ("  https://Example.COM/a?b=1  ", "https://Example.COM/a?b=1",
                                                            "example.com"),
    ("http://example.com:443/", "http://example.com:443/", "example.com"), ("example.com:8080", "https://example.com:8080",
                                                                           "example.com"),
])
def test_normalize_url(raw, url, host):
    assert normalize_url(raw) == (url, host)


@pytest.mark.parametrize("raw", ["", "   ", "file:///etc/passwd", "javascript:alert(1)", "data:text/html,x",
                                 "ftp://example.com", "http://user:pw@example.com", "http://[::1", "http://",
                                 "https://exa mple.com", "http://example.com/\r\nX: y", "https://" + "a" * 2050,
                                 "http://example.com:99999/", "chrome://settings", "view-source:https://example.com"])
def test_normalize_url_rejects(raw):
    with pytest.raises(ToolError) as e:
        normalize_url(raw)
    assert e.value.status == 422


@pytest.mark.parametrize("raw", [
    {"width": 1000}, {"delay": 30}, {"url": "x" * 3000}, {"evil": 1}, {"page_size": "B5"}, {"margin": "huge"},
    {"orientation": "diagonal"}, {"full_page": True}, {"format": "png"},
])
def test_bad_options(raw):
    with pytest.raises(ValidationError):
        HtmlToPdfOptions.model_validate(raw)


# --------------------------------------------------------------------------------------------------- runs
def test_url_to_pdf(ctx, fake_render):
    fake = fake_render(info={"final_url": "https://www.example.com/", "page_height": 4000, "main_status": 200})
    render.run_html_to_pdf(ctx, HtmlToPdfOptions(url="www.example.com", width=1920, delay=1.5))
    p = fake.calls[0]["payload"]
    assert p["url"] == "https://www.example.com" and "html" not in p and "output" not in p
    assert (p["viewport_width"], p["device_scale_factor"]) == (1920, 1)
    assert p["delay_ms"] == 1500 and p["hide_cookie_banners"] is True and p["javascript"] is True
    assert p["timeout_ms"] <= 40_000 and fake.calls[0]["deadline"] == ctx.deadline
    assert ctx.limits == [("render_host", "www.example.com")]
    assert ctx.outputs[0]["name"] == "www.example.com.pdf" and ctx.outputs[0]["mime"] == "application/pdf"
    assert ctx.data["host"] == "www.example.com" and ctx.data["page_height"] == 4000 and not ctx.warnings


def test_mobile_width_without_scripts(ctx, fake_render):
    fake = fake_render()
    render.run_html_to_pdf(ctx, HtmlToPdfOptions(url="https://example.com", width=390, block_cookie_banners=False,
                                                 javascript=False))
    p = fake.calls[0]["payload"]
    assert (p["viewport_width"], p["device_scale_factor"]) == (390, 2)
    assert p["hide_cookie_banners"] is False and p["javascript"] is False


def test_warnings(ctx, fake_render):
    fake_render(info={"main_status": 404, "egress_blocked": 3})
    render.run_html_to_pdf(ctx, HtmlToPdfOptions(url="example.com"))
    joined = " ".join(ctx.warnings)
    assert "HTTP 404" in joined and "private or local" in joined


def test_uploaded_html(tmp_path, fake_render):
    fake = fake_render()
    c = html_ctx(tmp_path, "\ufeff<h1>Grüße</h1>".encode())
    render.run_html_to_pdf(c, HtmlToPdfOptions())
    p = fake.calls[0]["payload"]
    assert p["html"] == "<h1>Grüße</h1>" and "url" not in p
    assert c.limits == [("renders", "")] and c.outputs[0]["name"] == "page.pdf"
    assert c.data["host"] is None and c.data["final_url"] is None


@pytest.mark.parametrize("raw,text", [(b"<p>caf\xe9</p>", "<p>café</p>"),
                                      ("<p>ü</p>".encode("utf-16"), "<p>ü</p>")])
def test_uploaded_html_encodings(tmp_path, fake_render, raw, text):
    fake = fake_render()
    render.run_html_to_pdf(html_ctx(tmp_path, raw), HtmlToPdfOptions())
    assert fake.calls[0]["payload"]["html"] == text


def test_url_and_file_together_or_neither(tmp_path, ctx, fake_render):
    fake = fake_render()
    with pytest.raises(ToolError) as e:
        render.run_html_to_pdf(html_ctx(tmp_path, b"<p>x</p>"), HtmlToPdfOptions(url="example.com"))
    assert e.value.status == 422 and "not both" in e.value.detail
    with pytest.raises(ToolError) as e:
        render.run_html_to_pdf(ctx, HtmlToPdfOptions())
    assert e.value.status == 422 and "address" in e.value.detail
    assert fake.calls == []


def test_neither_or_both_refused_at_submit_time():
    """The registry ``validate`` hook: POST /api/jobs answers 422 before queueing, not a failed job later."""
    t = render.FROM_HTML
    assert t.validate is not None
    with pytest.raises(ToolError) as e:
        t.validate([], t.options())
    assert e.value.status == 422 and "address" in e.value.detail
    with pytest.raises(ToolError) as e:
        t.validate([], t.options(url="ftp://example.com/x"))
    assert e.value.status == 422
    with pytest.raises(ToolError) as e:
        t.validate([object()], t.options(url="example.com"))
    assert "not both" in e.value.detail
    t.validate([], t.options(url="example.com"))  # fine
    t.validate([object()], t.options())  # an uploaded HTML file alone is fine


def test_oversized_html(tmp_path, fake_render, monkeypatch):
    fake = fake_render()
    monkeypatch.setattr(render, "HTML_MAX_BYTES", 10)
    with pytest.raises(ToolError) as e:
        render.run_html_to_pdf(html_ctx(tmp_path, b"<p>" + b"x" * 100 + b"</p>"), HtmlToPdfOptions())
    assert e.value.status == 413 and fake.calls == []


def test_pdf_payload(ctx, fake_render):
    fake = fake_render()
    render.run_html_to_pdf(ctx, HtmlToPdfOptions(url="https://example.com/news", page_size="Letter",
                                                 orientation="landscape", margin="big", single_page=False,
                                                 background=False))
    p = fake.calls[0]["payload"]
    assert p["url"] == "https://example.com/news"
    assert p["pdf"] == {"format": "Letter", "landscape": True,
                        "margin_mm": {"top": 20, "right": 20, "bottom": 20, "left": 20},
                        "print_background": False, "single_page": False, "media": "print"}
    assert ctx.outputs[0]["name"] == "example.com.pdf" and ctx.outputs[0]["mime"] == "application/pdf"


def test_pdf_defaults_are_one_long_screen_page(ctx, fake_render):
    fake = fake_render()
    render.run_html_to_pdf(ctx, HtmlToPdfOptions(url="example.com"))
    assert fake.calls[0]["payload"]["pdf"]["single_page"] is True
    assert fake.calls[0]["payload"]["pdf"]["media"] == "screen"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:fetch('http://169.254.169.254')",
                                 "gopher://127.0.0.1:6379/_FLUSHALL"])
def test_ssrf_schemes_never_reach_the_renderer(ctx, fake_render, url):
    fake = fake_render()
    with pytest.raises(ToolError):
        render.run_html_to_pdf(ctx, HtmlToPdfOptions(url=url))
    assert fake.calls == [] and ctx.limits == []


@pytest.mark.parametrize("url,reason", [("http://169.254.169.254/latest/meta-data/", "link-local"),
                                        ("http://127.0.0.1:8080/api/health", "port-not-allowed"),
                                        ("http://internal.example/", "private")])
def test_ssrf_refusals_from_the_stack_are_clear(ctx, fake_render, url, reason):
    """Private targets are refused by the renderer/egress; the user sees a clear message, the job fails 422."""
    fake_render(error=renderer._error(SimpleNamespace(
        status=422, json={"error": "blocked_target", "detail": reason}, header=lambda *_: "")))
    with pytest.raises(renderer.RenderError) as e:
        render.run_html_to_pdf(ctx, HtmlToPdfOptions(url=url))
    info = error_info(e.value)
    assert info["status"] == 422
    assert "heypdf" in info["detail"] or "standard ports" in info["detail"]
    assert ctx.outputs == []


def test_render_limits_are_charged_before_rendering(ctx, fake_render):
    fake = fake_render()

    def limited(host):
        raise ToolError(429, "Too many renders of that website — try again in a minute")
    ctx.check_render_host = limited
    with pytest.raises(ToolError) as e:
        render.run_html_to_pdf(ctx, HtmlToPdfOptions(url="example.com"))
    assert e.value.status == 429 and fake.calls == []


# ------------------------------------------------------------------------------------------ repository
def test_guard_copies_are_identical():
    assert (ROOT / "services/egress/guard.py").read_bytes() == (ROOT / "services/renderer/guard.py").read_bytes()


def test_playwright_is_pinned_and_hash_locked():
    req = (ROOT / "services/renderer/requirements.txt").read_text()
    lock = (ROOT / "services/renderer/requirements.lock").read_text()
    pin = re.search(r"^playwright==(\d+\.\d+\.\d+)$", req, re.M).group(1)
    assert pin == "1.63.0"  # released 2026-09-15 (>= 14 days); 1.64.0 becomes eligible on 2026-10-27
    assert f"playwright=={pin} \\" in lock and lock.count("--hash=sha256:") > 20


def test_service_images_pin_the_base_digest_and_run_non_root():
    for svc in ("docs", "renderer", "egress"):
        text = (ROOT / f"services/{svc}/Dockerfile").read_text()
        assert re.search(r"^ARG PYTHON_IMAGE=python:3\.12\.\d+-slim-trixie@sha256:[0-9a-f]{64}$", text, re.M), svc
        runtime = text.split("AS runtime", 1)[1]
        assert re.search(r"^USER (docs|renderer|egress)$", runtime, re.M) and "HEALTHCHECK" in runtime, svc
        assert text.rsplit("\nFROM ", 1)[1].startswith("base AS runtime"), svc  # runtime = last (default) stage


def test_seccomp_profile_allows_only_the_userns_additions():
    prof = json.loads((ROOT / "services/seccomp/chromium-userns.json").read_text())
    assert prof["defaultAction"] == "SCMP_ACT_ERRNO"
    unconditional = {n for rule in prof["syscalls"] if rule["action"] == "SCMP_ACT_ALLOW" and not rule.get("args")
                     and not rule.get("includes") and not rule.get("excludes") for n in rule["names"]}
    assert {"clone", "unshare", "chroot"} <= unconditional
    assert not {"mount", "ptrace", "kexec_load", "init_module", "bpf"} & unconditional


@pytest.mark.parametrize("compose", ["docker-compose.yml", "deploy/compose.prod.yml"])
def test_compose_files_isolate_the_services(compose):
    text = (ROOT / compose).read_text()
    for needle in ("docs-net:", "render-sbx:", "egress-wan:", "internal: true",
                   "com.docker.network.bridge.gateway_mode_ipv4: isolated", "seccomp=", "HEYP_RENDER_CHROMIUM_SANDBOX",
                   "HEYP_EGRESS_DENY_CIDRS", "HEYP_DOCS_URL", "HEYP_RENDERER_SOCKET", "read_only: true",
                   "cap_drop: [ALL]", "no-new-privileges:true"):
        assert needle in text, needle
    renderer_block = text.split("\n  renderer:", 1)[1].split("\n  egress:", 1)[0]
    assert "networks: [render-sbx]" in renderer_block and "docs-net" not in renderer_block


def test_tool_deadline_is_respected_by_the_clients(ctx, fake_render):
    fake = fake_render()
    ctx.deadline = time.monotonic() + 42
    render.run_html_to_pdf(ctx, HtmlToPdfOptions(url="example.com"))
    assert fake.calls[0]["deadline"] == ctx.deadline
