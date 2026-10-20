"""End-to-end SSRF + abuse tests: app (this container) -> UDS -> renderer -> egress proxy -> targets.

Every test also passes the autouse invariant in conftest.py: no request ever reached the internal service,
the fake cloud metadata (IPv4 169.254.169.254 + IPv6 fd00:ec2::254), the renderer's own loopback canary, or
the UDP sink next to Chromium.
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import CONTROL, L3, health, info, logs, page_height_pt, pdf_text, render

pytestmark = pytest.mark.skipif(CONTROL, reason="enforcement suite")
P = "http://public.fake"


def beacons(key):
    return [h for h in logs("web") if h["path"].startswith("/beacon") and key in h["path"]]


# ------------------------------------------------------------------------------------------- baseline
def test_public_page_renders():
    r = render(url=P + "/")
    assert "PUBLIC OK" in pdf_text(r)
    assert info(r)["main_status"] == 200


# ------------------------------------------------------------------------------- L4 pre-flight (400/422)
@pytest.mark.parametrize("url", ["file:///etc/passwd", "data:text/html,<h1>x</h1>", "javascript:alert(1)",
                                 "chrome://version", "view-source:http://public.fake/", "ftp://public.fake/",
                                 "gopher://public.fake/", "blob:http://public.fake/x", "ws://public.fake/",
                                 "http://user:pw@public.fake/", "http://public.fake/\r\nX: y"])
def test_preflight_rejects_schemes_and_userinfo(url):
    r = render(url=url)
    assert r.status_code == 400 and r.json()["error"] == "invalid_url", r.text


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://[::1]/", "http://localhost/", "http://LOCALHOST./",
                                 "http://169.254.169.254/latest/meta-data/", "http://[fd00:ec2::254]/",
                                 "http://[::ffff:169.254.169.254]/", "http://10.0.0.1/", "http://100.64.0.1/",
                                 "http://metadata.google.internal/", "http://host.docker.internal/",
                                 "http://public.fake:22/", "http://public.fake:6379/", "http://１２７.０.０.１/"])
def test_preflight_blocks_obvious_targets(url):
    r = render(url=url)
    assert r.status_code == 422 and r.json()["error"] == "blocked_target", r.text


# ----------------------------------------------------- L1 proxy: names/encodings that only resolve to bad IPs
@pytest.mark.parametrize("url,reason", [
    ("http://secret-app/", "private"), ("https://secret-app/", "private"), ("http://172.31.50.10/", "private"),
    ("http://meta.fake/", "link-local"), ("http://loopback.fake/", "loopback"), ("http://v6meta.fake/", "unique-local"),
    ("http://mapped.fake/", "private"), ("http://nat64.fake/", "nat64"), ("http://mixed.fake/", "private"),
    ("http://cgnat.fake/", "cgnat"), ("http://0x7f.1/", "loopback"), ("http://2130706433/", "loopback"),
    ("http://0177.0.0.1/", "loopback"), ("http://127.1/", "loopback"), ("http://egress:3129/health", "port-not-allowed"), ("http://egress/", "private"),
    ("http://udp-sink/", "private"), ("http://nonexistent.fake/", "dns-failure"),
])
def test_proxy_blocks_resolved_targets(url, reason):
    r = render(url=url)
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"] == "blocked_target"
    assert reason in body["detail"], body


# ------------------------------------------------------------------------------------- redirects
@pytest.mark.parametrize("to", ["http://secret-app/", "https://secret-app/", "http://169.254.169.254/latest/meta-data/",
                                "http://[fd00:ec2::254]/", "http://loopback.fake/", "http://127.0.0.1:9999/",
                                "http://0x7f000001:9999/", "http://localhost:9999/", "http://[::1]:9999/",
                                "http://11.11.11.10:22/", "http://rebind.fake/x"])
def test_redirect_to_blocked_target(to):
    for _ in range(2):  # twice: rebind.fake alternates public/private answers
        r = render(url=f"{P}/redirect?to={to}")
        assert r.status_code in (200, 422) or "ERR_UNSAFE_PORT" in r.text, r.text  # Chromium blocks :22 itself
        if r.status_code == 200:  # only possible for rebind.fake on its public answer
            assert to.startswith("http://rebind.fake")


@pytest.mark.parametrize("to", ["file:///etc/passwd", "data:text/html,<h1>DATA-PWNED</h1>", "chrome://version",
                                "view-source:http://secret-app/", "javascript:alert(1)"])
def test_redirect_to_non_web_scheme(to):
    r = render(url=f"{P}/redirect?to={to}")
    assert r.status_code in (422, 502), r.text  # Chromium: ERR_UNSAFE_REDIRECT / ERR_INVALID_REDIRECT


# ------------------------------------------------------------------------------- sub-resources & JS
def test_subresources_and_js_requests_are_blocked():
    r = render(url=P + "/ssrf-subresources", delay_ms=1500)
    assert r.status_code == 200
    assert beacons("subres=done"), "page JS did not run - test would be meaningless"
    assert info(r)["egress_blocked"] >= 10


def test_dns_rebinding_never_reaches_internal():
    """rebind.fake answers 11.11.11.10 and 172.31.50.10 alternately with TTL 0 (see targets.py)."""
    statuses = [render(url="http://rebind.fake/").status_code for _ in range(8)]
    assert set(statuses) == {200, 422}, statuses  # both answers were seen; private ones were refused
    r = render(html="".join(f'<img src="http://rebind.fake/img{i}">' for i in range(10)), delay_ms=500)
    assert r.status_code == 200
    rebind_hits = [h for h in logs("web") if h["path"].startswith("/img")]
    assert 0 < len(rebind_hits) < 10  # public answers went to the public server, private ones were refused


def test_dns_rebinding_tuned_for_toctou():
    """rebind3.fake: 2 public answers then 1 private -- defeats "pre-flight + route check + continue_()" 10/10
    (naive_rebind.py). The proxy resolves exactly once per connection and connects to that IP."""
    for _ in range(9):
        render(url="http://rebind3.fake/")
    render(html="".join(f'<img src="http://rebind3.fake/r3-{i}">' for i in range(12)), delay_ms=500)


def test_websockets():
    r = render(url=P + "/ws-attack", delay_ms=1500)
    assert r.status_code == 200
    ws_upgrades = [h for h in logs("web") if h.get("upgrade")]
    if L3:
        assert ws_upgrades == []  # route_web_socket mocks every socket: nothing leaves the browser
    else:
        assert ws_upgrades, "proxy-only mode: public ws should pass the proxy (positive control)"


def test_webrtc():
    r = render(url=P + "/webrtc-attack", delay_ms=2500)
    assert r.status_code == 200
    api = beacons("rtc_api=")
    assert api, "page JS did not run"
    if L3:
        assert "rtc_api=undefined" in api[0]["path"]
    cands = [h["path"] for h in beacons("cand=")]
    # disable_non_proxied_udp: no host/srflx/relay candidates over UDP at all
    assert not [c for c in cands if "udp" in c.lower()], cands


def test_webrtc_in_iframe():
    r = render(url=P + "/webrtc-iframe", delay_ms=1500)
    assert r.status_code == 200
    if L3:
        assert all("undefined" in h["path"] for h in beacons("iframe_rtc_api="))


def test_popups_never_open():
    r = render(url=P + "/popup", delay_ms=1000)
    assert r.status_code == 200
    if L3:
        assert not [h for h in logs("web") if "popup-target-open" in h["path"]]
        assert info(r)["popups"] <= 1  # the target=_blank click: opened and closed immediately


# ------------------------------------------------------------------------------- downloads
def test_top_level_download_is_refused():
    r = render(url=P + "/download")
    assert r.status_code == 422 and r.json()["error"] == "not_a_page", r.text


def test_scripted_download_is_dropped():
    r = render(url=P + "/download-click", delay_ms=1000)
    assert r.status_code == 200  # downloads are denied by the context (accept_downloads=False)


# ------------------------------------------------------------------------------- scheme escapes in-page
@pytest.mark.parametrize("page", ["/scheme-file", "/scheme-data", "/scheme-chrome", "/scheme-viewsource"])
def test_page_cannot_navigate_to_local_schemes(page):
    r = render(url=P + page, delay_ms=500)
    assert r.status_code in (200, 422), r.text
    if r.status_code == 200:
        assert info(r)["final_url"] == P + page


def test_local_scheme_iframes():
    r = render(url=P + "/scheme-iframes", delay_ms=800)
    assert r.status_code == 200


def test_html_input_cannot_read_files_or_reach_internal():
    html = ('<meta http-equiv="refresh" content="0;url=file:///etc/passwd"><iframe src="file:///etc/passwd"></iframe>'
            '<img src="http://secret-app/html-img"><link rel=stylesheet href="file:///etc/hostname">'
            '<iframe src="http://169.254.169.254/"></iframe><h1>html</h1>')
    r = render(html=html, delay_ms=800)
    assert r.status_code in (200, 422), r.text
    if r.status_code == 200:
        assert info(r)["final_url"] == "about:blank"


# ------------------------------------------------------------------------------- resource limits
def test_slow_server_times_out():
    t = time.time()
    r = render(url=P + "/slow", timeout_ms=3000)
    assert r.status_code == 504 and time.time() - t < 8


def test_infinite_js_loop_times_out_and_renderer_survives():
    t = time.time()
    r = render(url=P + "/infinite", timeout_ms=3000)
    assert r.status_code in (504, 502) and time.time() - t < 15, r.text
    assert render(url=P + "/").status_code == 200


def test_memory_bomb_is_contained():
    r = render(url=P + "/memory-bomb", timeout_ms=20000)
    assert r.status_code in (502, 504, 500), r.status_code
    assert health()["ok"]
    assert render(url=P + "/").status_code == 200


def test_byte_budget():
    t = time.time()
    r = render(url=P + "/huge", timeout_ms=20000)
    assert r.status_code == 413 and r.json()["error"] == "too_large", r.text
    assert time.time() - t < 20


def test_navigation_loop():
    r = render(url=P + "/nav-loop", timeout_ms=8000, delay_ms=2000)
    assert r.status_code in (422, 504), r.text


def test_single_page_is_capped_at_200_inches():
    r = render(url=P + "/tall?h=100000", pdf={"single_page": True, "media": "screen"})
    assert r.status_code == 200 and abs(page_height_pt(r) - (200 * 72 + 1) * 0.75) < 2


def test_concurrency_limit_returns_503():
    with ThreadPoolExecutor(10) as ex:
        rs = list(ex.map(lambda _: render(url=P + "/", delay_ms=2500), range(10)))
    codes = [r.status_code for r in rs]
    assert codes.count(503) >= 4 and codes.count(200) >= 2, codes
    assert all(r.headers.get("retry-after") for r in rs if r.status_code == 503)


# ------------------------------------------------------------------------------- features
def test_javascript_off():
    assert render(url=P + "/js-beacon", javascript=False).status_code == 200
    time.sleep(0.3)
    assert not beacons("js=1") and beacons("noscript=1")
    assert render(url=P + "/js-beacon", javascript=True).status_code == 200
    time.sleep(0.3)
    assert beacons("js=1")


def test_cookie_banner_hiding():
    on = pdf_text(render(url=P + "/cookie-banner", hide_cookie_banners=True, pdf={"media": "screen"}))
    off = pdf_text(render(url=P + "/cookie-banner", hide_cookie_banners=False, pdf={"media": "screen"}))
    assert "We value your privacy" in off and "We value your privacy" not in on and "content" in on


@pytest.mark.parametrize("fmt,landscape,box", [("A4", False, (595, 842)), ("A4", True, (842, 595)),
                                               ("Letter", False, (612, 792))])
def test_pdf_page_size(fmt, landscape, box):
    r = render(url=P + "/print-css",
               pdf={"format": fmt, "landscape": landscape, "margin_mm": {"top": 20, "right": 15, "bottom": 20, "left": 15}})
    assert r.status_code == 200 and r.content.startswith(b"%PDF-")
    m = re.search(rb"/MediaBox\s*\[\s*0 0 ([\d.]+) ([\d.]+)\s*\]", r.content)
    assert m, "no MediaBox"
    w, h = float(m.group(1)), float(m.group(2))
    assert abs(w - box[0]) < 2 and abs(h - box[1]) < 2, (w, h)


def test_pdf_single_page_screen_media():
    r = render(url=P + "/tall?h=3000", pdf={"single_page": True, "media": "screen", "margin_mm":
               {"top": 0, "right": 0, "bottom": 0, "left": 0}})
    assert r.status_code == 200
    assert r.content.count(b"/Type /Page\n") + r.content.count(b"/Type/Page") <= 2


def test_cmp_scroll_lock_is_undone_for_the_whole_page():
    r = render(url=P + "/cookie-lock", hide_cookie_banners=True, pdf={"single_page": True, "media": "screen"})
    assert r.status_code == 200 and abs(page_height_pt(r) - 5001 * 0.75) < 2, page_height_pt(r)
