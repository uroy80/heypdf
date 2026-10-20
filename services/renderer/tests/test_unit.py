"""Pre-flight URL checks, request validation and error sanitising (no browser needed)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

import renderer
from renderer import RenderError, RenderRequest, navigation_detail, validate_url


@pytest.mark.parametrize("url,expected", [
    ("example.com", "https://example.com"), ("https://example.com/a?b=c#d", "https://example.com/a?b=c#d"),
    ("http://example.com/", "http://example.com/"), ("  https://example.com  ", "https://example.com"),
    ("example.com/a:b", "https://example.com/a:b"),
])
def test_valid_urls(url, expected):
    assert validate_url(url) == expected


@pytest.mark.parametrize("url", ["file:///etc/passwd", "data:text/html,<h1>x</h1>", "javascript:alert(1)",
                                 "chrome://version", "view-source:http://example.com/", "ftp://example.com/",
                                 "gopher://example.com/", "blob:http://example.com/x", "ws://example.com/",
                                 "http://user:pw@example.com/", "http://example.com/\r\nX: y", "http://",
                                 "https://" + "a" * 2050, "http://[::1/"])
def test_invalid_urls(url):
    with pytest.raises(RenderError) as e:
        validate_url(url)
    assert e.value.status == 400 and e.value.code == "invalid_url"


@pytest.mark.parametrize("url,reason", [
    ("http://127.0.0.1/", "loopback"), ("http://[::1]/", "loopback"), ("http://localhost/", "internal-name"),
    ("http://LOCALHOST./", "internal-name"), ("http://169.254.169.254/latest/meta-data/", "link-local"),
    ("http://[fd00:ec2::254]/", "unique-local"), ("http://[::ffff:169.254.169.254]/", "link-local"),
    ("http://10.0.0.1/", "private"), ("http://100.64.0.1/", "cgnat"), ("http://metadata.google.internal/", "internal-name"),
    ("http://host.docker.internal/", "internal-name"), ("http://example.com:22/", "port-not-allowed"),
    ("example.com:6379", "port-not-allowed"), ("http://１２７.０.０.１/", "loopback"), ("http://0.0.0.0/", "this-network"),
])
def test_blocked_targets(url, reason):
    with pytest.raises(RenderError) as e:
        validate_url(url)
    assert (e.value.status, e.value.code, e.value.detail) == (422, "blocked_target", reason)


def test_request_needs_exactly_one_source():
    with pytest.raises(ValidationError):
        RenderRequest()
    with pytest.raises(ValidationError):
        RenderRequest(url="https://example.com", html="<p>x</p>")
    req = RenderRequest(html="<p>x</p>")
    assert (req.pdf.format, req.pdf.single_page, req.pdf.media) == ("A4", False, "print")


@pytest.mark.parametrize("bad", [{"viewport_width": 100}, {"viewport_width": 10000}, {"device_scale_factor": 4},
                                 {"delay_ms": 60_000}, {"timeout_ms": 120_000}, {"pdf": {"format": "B5"}},
                                 {"pdf": {"margin_mm": {"top": 80}}}, {"pdf": {"scale": 3}}, {"unknown": 1},
                                 {"output": "png"}, {"full_page": True}])  # PDF only: no screenshot options
def test_request_bounds(bad):
    with pytest.raises(ValidationError):
        RenderRequest(html="<p>x</p>", **bad)


def test_html_size_cap():
    with pytest.raises(ValidationError):
        RenderRequest(html="x" * (renderer.MAX_HTML_CHARS + 1))


@pytest.mark.parametrize("msg,expected", [
    ("Page.goto: net::ERR_NAME_NOT_RESOLVED at https://secret.example/?token=abc", "net::ERR_NAME_NOT_RESOLVED"),
    ("Page.goto: net::ERR_CERT_AUTHORITY_INVALID at https://x/", "net::ERR_CERT_AUTHORITY_INVALID"),
    ("Page.goto: Target page, context or browser has been closed\nCall log: navigating to https://x/", 
     "the page could not be loaded"),
    ("", "the page could not be loaded"),
])
def test_navigation_errors_never_echo_urls(msg, expected):
    assert navigation_detail(msg) == expected


def test_chromium_is_forced_through_the_proxy():
    args = " ".join(renderer.CHROMIUM_ARGS)
    assert "--proxy-bypass-list=<-loopback>" in args
    assert "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE egress" in args
    assert "--disable-quic" in args and "disable_non_proxied_udp" in args
