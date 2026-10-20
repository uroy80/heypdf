"""The proxy in-process, attacked over raw sockets the way a compromised renderer would.

Blocked targets must be refused by policy BEFORE any connect(); a positive control (policy off) proves the same
requests would otherwise go through, so the refusals are the policy's doing.
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import PORTS
from guard import Policy

import egress_proxy as ep

DENY, CONTROL, SLOT0, SLOT1, SLOT2 = PORTS
SECRET = "test-secret-0123456789"


@pytest.fixture(scope="module", autouse=True)
def proxy():
    loop = asyncio.new_event_loop()
    started = threading.Event()

    async def run():
        servers = await ep.start("127.0.0.1")
        started.set()
        await asyncio.gather(*(s.serve_forever() for s in servers))

    t = threading.Thread(target=loop.run_until_complete, args=(run(),), daemon=True)
    t.start()
    assert started.wait(10)
    yield
    loop.call_soon_threadsafe(loop.stop)


@pytest.fixture(autouse=True)
def fresh_slots():
    for i in range(3):
        control("POST", f"/slots/{i}")
    yield


def raw(request: bytes, port: int = SLOT0, read: int = 1 << 24, timeout: float = 10) -> bytes:
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.sendall(request)
    out = b""
    try:
        while len(out) < read:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
    except (socket.timeout, ConnectionError):
        pass
    s.close()
    return out


def status(resp: bytes) -> int:
    return int(resp.split(b" ", 2)[1])


def control(method: str, path: str, secret: str | None = SECRET) -> tuple[int, dict]:
    hdr = f"X-Egress-Secret: {secret}\r\n" if secret is not None else ""
    r = raw(f"{method} {path} HTTP/1.1\r\nHost: egress\r\n{hdr}\r\n".encode(), port=CONTROL)
    return status(r), json.loads(r.split(b"\r\n\r\n", 1)[1])


def stats(slot: int = 0) -> dict:
    return control("GET", f"/slots/{slot}")[1]


# ------------------------------------------------------------------------------------------------ control API
def test_healthz_needs_no_secret():
    assert control("GET", "/healthz", secret=None) == (200, {"status": "ok"})


@pytest.mark.parametrize("secret", [None, "", "wrong", SECRET + "x"])
def test_control_api_needs_the_secret(secret):
    code, body = control("DELETE", "/slots/0", secret=secret)
    assert code == 403 and body == {"error": "secret"}


def test_control_api_bounds():
    assert control("GET", "/slots/3")[0] == 404 and control("GET", "/slots/-1")[0] == 404
    assert control("PUT", "/slots/0")[0] == 404


# ---------------------------------------------------------------------------------------------- enforcement
def test_deny_port_refuses_everything():
    for req in (b"CONNECT example.com:443 HTTP/1.1\r\n\r\n", b"GET http://example.com/ HTTP/1.1\r\n\r\n"):
        r = raw(req, port=DENY)
        assert status(r) == 403 and b"no-job" in r


@pytest.mark.parametrize("target,reason", [
    ("127.0.0.1:443", "loopback"), ("[::1]:443", "loopback"), ("0.0.0.0:443", "this-network"),
    ("10.0.0.1:443", "private"), ("172.17.0.1:80", "private"), ("192.168.1.1:443", "private"),
    ("169.254.169.254:80", "link-local"), ("[fd00:ec2::254]:80", "unique-local"),
    ("[::ffff:169.254.169.254]:80", "link-local"), ("[::ffff:a9fe:a9fe]:80", "link-local"),
    ("[64:ff9b::a9fe:a9fe]:80", "nat64"), ("100.64.0.1:443", "cgnat"), ("203.0.114.7:443", "denied-cidr"),
    ("localhost:443", "internal-name"), ("metadata.google.internal:80", "internal-name"),
    ("host.docker.internal:443", "internal-name"), ("1.1.1.1:22", "port-not-allowed"),
    ("1.1.1.1:6379", "port-not-allowed"), ("nonexistent.invalid:443", "dns-failure"),
])
def test_connect_blocked_by_policy(target, reason):
    r = raw(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    assert status(r) == 403, r[:200]
    assert f"reason={reason}".encode() in r, r[:300]
    assert stats()["blocked"][-1]["reason"] == reason


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/",
                                 "http://[fd00:ec2::254]/", "http://1.1.1.1@127.0.0.1/", "http://127.0.0.1#@1.1.1.1/",
                                 "http://localhost:8080/"])
def test_absolute_form_blocked(url):
    assert status(raw(f"GET {url} HTTP/1.1\r\nHost: example.com\r\n\r\n".encode())) == 403


@pytest.mark.parametrize("url", ["ftp://example.com/", "file:///etc/passwd", "https://example.com/", "/relative"])
def test_non_http_absolute_forms_refused(url):
    assert status(raw(f"GET {url} HTTP/1.1\r\n\r\n".encode())) == 400


def test_bad_request_line():
    assert status(raw(b"GARBAGE\r\n\r\n")) == 400


def test_connection_cap_per_job():
    codes = [status(raw(b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n", port=SLOT1)) for _ in range(22)]
    assert codes[:20] == [403] * 20 and codes[20:] == [429, 429]


def test_slots_are_accounted_separately():
    raw(b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n", port=SLOT2)
    assert stats(2)["conns"] == 1 and stats(0)["conns"] == 0
    control("POST", "/slots/2")
    assert stats(2)["conns"] == 0 and stats(2)["blocked"] == []


# ----------------------------------------------------------------------------- positive control (policy off)
class _Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/huge":
            self.send_response(200)
            self.send_header("Content-Length", str(8 * 1024 * 1024))
            self.end_headers()
            try:
                for _ in range(128):
                    self.wfile.write(b"x" * 65536)
            except OSError:
                pass
            return
        body = f"UPSTREAM OK host={self.headers.get('Host')}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def policy_off(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(ep, "policy", Policy(enabled=False, deny_local_nets=False))
    yield server.server_address[1]
    server.shutdown()


def test_positive_control_same_request_passes_without_policy(policy_off):
    port = policy_off
    r = raw(f"GET http://127.0.0.1:{port}/ HTTP/1.1\r\nHost: retarget.example\r\n\r\n".encode())
    assert status(r) == 200 and b"UPSTREAM OK" in r
    # The Host header travels as data; the upstream was chosen from the request-target only.
    assert b"host=retarget.example" in r
    t = raw(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\nGET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
    assert t.startswith(b"HTTP/1.1 200 Connection established") and b"UPSTREAM OK" in t


def test_byte_budget_cuts_the_job_off(policy_off):
    t = time.monotonic()
    r = raw(f"GET http://127.0.0.1:{policy_off}/huge HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert len(r) < 3 * 1024 * 1024 and time.monotonic() - t < 10
    assert stats()["over_budget"] is True
    # ...and the slot refuses new connections until the next job resets it.
    assert status(raw(f"GET http://127.0.0.1:{policy_off}/ HTTP/1.1\r\n\r\n".encode())) == 413


# ------------------------------------------------------------------------------------- logs (privacy)
def _log_lines(text: str) -> list[dict]:
    return [json.loads(ln) for ln in text.splitlines() if ln.startswith("{")]


def test_logs_never_name_the_destination_by_default(capsys, policy_off):
    """/privacy says capture addresses aren't kept: the per-connection log has no host and no IP."""
    assert ep.LOG_HOSTS is False  # the default (HEYP_EGRESS_LOG_HOSTS unset)
    capsys.readouterr()
    raw(f"GET http://127.0.0.1:{policy_off}/ HTTP/1.1\r\nHost: secret-site.example\r\n\r\n".encode())
    raw(b"CONNECT nonexistent-secret.invalid:443 HTTP/1.1\r\n\r\n", port=SLOT1)
    time.sleep(0.2)
    out = capsys.readouterr().out
    lines = _log_lines(out)
    verdicts = {ln.get("verdict") for ln in lines}
    assert {"allow", "block"} <= verdicts, out
    for ln in lines:
        assert not {"host", "ip", "ips"} & set(ln), ln
    assert "secret" not in out and "127.0.0.1" not in out and ".invalid" not in out
    allow = next(ln for ln in lines if ln["verdict"] == "allow")
    assert {"slot", "m", "port", "up", "down", "ms", "ts"} <= set(allow)
    block = next(ln for ln in lines if ln["verdict"] == "block")
    assert block["reason"] == "dns-failure"
    # The job's own stats (returned to the renderer over the control API, never logged) still name the host.
    assert stats(1)["blocked"][-1]["host"] == "nonexistent-secret.invalid"


def test_host_logging_is_an_explicit_opt_in(capsys, monkeypatch):
    monkeypatch.setattr(ep, "LOG_HOSTS", True)
    capsys.readouterr()
    raw(b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n")
    time.sleep(0.2)
    block = [ln for ln in _log_lines(capsys.readouterr().out) if ln.get("verdict") == "block"][-1]
    assert block["host"] == "127.0.0.1" and block["reason"] == "loopback"
