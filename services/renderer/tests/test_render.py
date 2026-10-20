"""Real renders of inline HTML through the HTTP API (Chromium in this image; no network at all)."""
from __future__ import annotations

import re

import pytest
from conftest import info, pdf_text

import renderer

GREEN = "<body style='margin:0;background:#0a0'><h1 style='color:#fff'>heypdf</h1></body>"


def render(client, **kw):
    return client.post("/v1/render", json=kw)


def media_box(data: bytes) -> tuple[float, float]:
    m = re.search(rb"/MediaBox\s*\[\s*0 0 ([\d.]+) ([\d.]+)\s*\]", data)
    assert m, "no MediaBox"
    return float(m.group(1)), float(m.group(2))


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    h = client.get("/v1/health").json()
    assert h["ok"] is True and h["version"] and h["slots"] == 1


def test_pdf(client):
    r = render(client, html=GREEN)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.headers["cache-control"] == "no-store" and r.content.startswith(b"%PDF-")
    assert "heypdf" in pdf_text(r)
    i = info(r)
    assert i["final_url"] == "about:blank" and i["viewport_width"] == 1366 and i["page_height"] > 0


@pytest.mark.parametrize("fmt,landscape,box", [("A4", False, (595, 842)), ("A4", True, (842, 595)),
                                               ("Letter", False, (612, 792))])
def test_pdf_page_sizes(client, fmt, landscape, box):
    r = render(client, html="<h1>print me</h1>", pdf={"format": fmt, "landscape": landscape})
    assert r.status_code == 200 and r.content.startswith(b"%PDF-")
    w, h = media_box(r.content)
    assert abs(w - box[0]) < 2 and abs(h - box[1]) < 2


def test_pdf_single_page(client):
    tall = "<body style='margin:0'><div style='height:3000px;background:#00f'></div></body>"
    r = render(client, html=tall, pdf={"single_page": True, "media": "screen",
                                       "margin_mm": {"top": 0, "right": 0, "bottom": 0, "left": 0}})
    assert r.status_code == 200
    assert r.content.count(b"/Type /Page\n") + r.content.count(b"/Type/Page") <= 2
    assert abs(media_box(r.content)[1] - 3001 * 0.75) < 2  # the whole page on one sheet (CSS px → pt)


def test_single_page_is_capped_at_200_inches(client):
    tall = "<body style='margin:0'><div style='height:60000px;background:#00f'></div></body>"
    r = render(client, html=tall, pdf={"single_page": True, "media": "screen"})
    assert r.status_code == 200 and abs(media_box(r.content)[1] - (200 * 72 + 1) * 0.75) < 2


def test_javascript_off_still_renders(client):
    page = "<body><p id='p'>static text</p><script>document.getElementById('p').textContent = 'scripted text'</script></body>"
    on, off = render(client, html=page), render(client, html=page, javascript=False)
    assert "scripted text" in pdf_text(on) and "static text" in pdf_text(off)


def test_webrtc_and_popups_are_gone(client):
    page = ("<body><p id='p'>exposed</p><script>"
            "const ok = typeof RTCPeerConnection === 'undefined' && window.open('https://example.com') === null;"
            "if (ok) document.getElementById('p').textContent = 'contained';</script></body>")
    assert "contained" in pdf_text(render(client, html=page))


def test_cookie_banners_hidden(client):
    page = ("<body><h1>content</h1><div id='onetrust-banner-sdk' style='position:fixed;inset:0;background:#f00'>"
            "We value your privacy</div></body>")
    on = pdf_text(render(client, html=page, pdf={"media": "screen"}))
    off = pdf_text(render(client, html=page, hide_cookie_banners=False, pdf={"media": "screen"}))
    assert "We value your privacy" in off and "We value your privacy" not in on and "content" in on


def test_subresources_to_internal_targets_do_not_break_the_render(client):
    page = ("<img src='http://169.254.169.254/latest/meta-data/'><img src='http://127.0.0.1:9/x'>"
            "<iframe src='file:///etc/passwd'></iframe><h1>still here</h1>")
    r = render(client, html=page)
    assert r.status_code == 200 and info(r)["final_url"] == "about:blank" and "still here" in pdf_text(r)


def test_url_preflight_errors(client):
    r = render(client, url="file:///etc/passwd")
    assert r.status_code == 400 and r.json() == {"error": "invalid_url",
                                                 "detail": "only http and https URLs can be rendered"}
    r = render(client, url="http://169.254.169.254/")
    assert r.status_code == 422 and r.json()["error"] == "blocked_target"


def test_unreachable_egress_is_a_clean_502(client):
    """No network here: the page cannot load; the answer names the Chromium error only, never the URL."""
    r = render(client, url="https://www.example.com/private?token=s3cret", timeout_ms=8000)
    assert r.status_code in (502, 504), r.text
    assert "s3cret" not in r.text and "example.com" not in r.text


def test_bad_requests_are_400(client):
    r = client.post("/v1/render", json={"url": "https://example.com", "html": "<p>x</p>"})
    assert r.status_code == 400 and r.json()["error"] == "bad_request"
    r = client.post("/v1/render", json={"html": "<p>x</p>", "evil": True})
    assert r.status_code == 400 and "evil" in r.json()["detail"]
    r = client.post("/v1/render", json={"html": "<p>x</p>", "output": "png"})
    assert r.status_code == 400 and "output" in r.json()["detail"]  # PDF only


def test_output_size_cap(client, monkeypatch):
    monkeypatch.setattr(renderer, "MAX_OUTPUT_BYTES", 1000)
    r = render(client, html=GREEN)
    assert r.status_code == 413 and r.json()["error"] == "too_large"


def test_full_queue_is_503_with_retry_after(client, monkeypatch):
    monkeypatch.setattr(renderer.R, "waiting", renderer.MAX_CONCURRENT + renderer.MAX_QUEUE)
    r = render(client, html=GREEN)
    assert r.status_code == 503 and r.headers["retry-after"] == "5" and r.json()["error"] == "busy"


def test_renders_keep_working_after_errors(client):
    render(client, url="http://127.0.0.1/")
    assert render(client, html=GREEN).status_code == 200
