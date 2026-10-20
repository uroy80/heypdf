"""Talk to the egress proxy directly, as a fully compromised renderer would (run on the sandbox network).

docker run --rm --network hs-urlrender_sandbox --network hs-urlrender_logs ... pytest test_proxy_raw.py
"""
import json
import socket
import time

import httpx
import pytest

PROXY = ("egress", 3130)  # slot 0
SECRET = "e2e-test-secret-0123456789"


def raw(request: bytes, port=3130, read=65536) -> bytes:
    s = socket.create_connection(("egress", port), timeout=10)
    s.sendall(request)
    out = b""
    try:
        while len(out) < read:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
    except socket.timeout:
        pass
    s.close()
    return out


def status(resp: bytes) -> int:
    return int(resp.split(b" ", 2)[1])


def secret_hits():
    return httpx.get("http://secret-app/log").json() + httpx.get("http://metadata/log").json()


@pytest.fixture(autouse=True)
def reset():
    httpx.get("http://secret-app/log/reset")
    httpx.get("http://metadata/log/reset")
    yield
    assert secret_hits() == []


@pytest.mark.parametrize("target", [
    "127.0.0.1:80", "[::1]:80", "0x7f.1:80", "2130706433:80", "017700000001:80", "127.1:80", "0:80", "0.0.0.0:80",
    "secret-app:80", "secret-app:443", "172.31.50.10:80", "169.254.169.254:80", "[fd00:ec2::254]:80",
    "[::ffff:169.254.169.254]:80", "[::ffff:a9fe:a9fe]:80", "[64:ff9b::a9fe:a9fe]:80", "meta.fake:80",
    "rebind.fake:80", "mixed.fake:80", "egress:3129", "localhost:3129", "public.fake:22", "public.fake:6379",
    "11.11.11.10:8080", "[fe80::1%25eth0]:80", "metadata.google.internal:80",
])
def test_connect_blocked(target):
    for _ in range(2):  # rebind.fake alternates; both answers must not reach 172.31.50.10
        r = raw(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        if target.startswith("rebind") and status(r) == 200:
            continue  # its public answer is allowed (and connects to the vetted 11.11.11.10)
        assert status(r) in (400, 403), r[:200]


@pytest.mark.parametrize("url", [
    "http://secret-app/", "http://172.31.50.10/", "http://169.254.169.254/latest/meta-data/",
    "http://[fd00:ec2::254]/", "http://public.fake@secret-app/", "http://secret-app#@public.fake/",
    "http://0x7f.1/", "http://loopback.fake/", "http://v6meta.fake/", "http://nat64.fake/",
])
def test_absolute_form_blocked(url):
    r = raw(f"GET {url} HTTP/1.1\r\nHost: public.fake\r\n\r\n".encode())
    assert status(r) in (400, 403), r[:200]


def test_host_header_cannot_retarget():
    """The upstream is chosen from the request-target only; Host just travels to the vetted server."""
    r = raw(b"GET http://public.fake/ HTTP/1.1\r\nHost: secret-app\r\n\r\n")
    assert status(r) == 200 and b"PUBLIC OK" in r


def test_smuggled_second_request_stays_on_vetted_upstream():
    req = (b"GET http://public.fake/ HTTP/1.1\r\nHost: public.fake\r\nContent-Length: 0\r\n\r\n"
           b"GET http://secret-app/ HTTP/1.1\r\nHost: secret-app\r\n\r\n")
    raw(req)  # the second request is just bytes to 11.11.11.10 (or dropped); never a new upstream


def test_non_http_absolute_schemes_refused():
    for u in ("ftp://public.fake/", "file:///etc/passwd", "gopher://public.fake/", "https://public.fake/"):
        assert status(raw(f"GET {u} HTTP/1.1\r\n\r\n".encode())) == 400


def test_deny_port_refuses_everything():
    assert status(raw(b"CONNECT public.fake:80 HTTP/1.1\r\n\r\n", port=3128)) == 403
    assert status(raw(b"GET http://public.fake/ HTTP/1.1\r\n\r\n", port=3128)) == 403


def test_public_connect_allowed_and_pinned():
    r = raw(b"CONNECT public.fake:80 HTTP/1.1\r\n\r\nGET / HTTP/1.1\r\nHost: public.fake\r\nConnection: close\r\n\r\n")
    assert r.startswith(b"HTTP/1.1 200 Connection established") and b"PUBLIC OK" in r


def test_byte_budget_and_kill():
    s = socket.create_connection(("egress", 3131), timeout=30)
    s.sendall(b"GET http://public.fake/huge HTTP/1.1\r\nHost: public.fake\r\n\r\n")
    got = 0
    while True:
        c = s.recv(1 << 20)
        if not c:
            break
        got += len(c)
    assert got < 25_000_000  # EGRESS_MAX_JOB_BYTES=20 MB in the test bed
    ctl = socket.create_connection(("egress", 3129), timeout=5)
    ctl.sendall(f"GET /slots/1 HTTP/1.1\r\nX-Egress-Secret: {SECRET}\r\n\r\n".encode())
    stats = json.loads(ctl.recv(65536).split(b"\r\n\r\n", 1)[1])
    assert stats["over_budget"] is True


def test_control_api_needs_secret():
    ctl = socket.create_connection(("egress", 3129), timeout=5)
    ctl.sendall(b"DELETE /slots/0 HTTP/1.1\r\n\r\n")
    assert b"403" in ctl.recv(1000).split(b"\r\n")[0]


def test_slowloris_head_times_out():
    s = socket.create_connection(("egress", 3132), timeout=20)
    s.sendall(b"GET http://public.fake/ HTTP/1.1\r\n")
    t = time.time()
    assert s.recv(100) == b"" and time.time() - t < 15


def test_control_healthz_needs_no_secret():
    ctl = socket.create_connection(("egress", 3129), timeout=5)
    ctl.sendall(b"GET /healthz HTTP/1.1\r\n\r\n")
    data = ctl.recv(1000)
    assert b" 200 " in data.split(b"\r\n")[0] and b'"status": "ok"' in data
