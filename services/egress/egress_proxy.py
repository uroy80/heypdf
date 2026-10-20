"""heypdf-egress: the single choke point between the sandboxed browser (heypdf-renderer) and the internet.

* HTTP forward proxy (absolute-form GET/POST/...) + CONNECT tunnels (https, ws, wss).
* Resolve ONCE, check EVERY resolved address against guard.Policy, then connect() to the vetted IP itself.
  There is no second resolution anywhere, so DNS rebinding / TOCTOU cannot swap the target.
* One listening port per render slot (BASE_PORT + i). The renderer gives each browser context the port of the
  slot it holds, so accounting needs no Proxy-Authorization round-trips (Playwright does not answer 407s for the
  initial about:blank document). Per slot: byte budget (both directions), connection cap, tunnel lifetime, and
  the list of blocked attempts.
* DENY_PORT (3128) answers 403 to everything: the browser-level (launch) proxy points there, so nothing outside
  a slot's context can reach the network.
* Control port (3129): ``GET /healthz`` → ``{"status": "ok"}`` (no secret; for the container healthcheck), and
  with the ``X-Egress-Secret`` header: POST /slots/<i> → reset for a new job, GET /slots/<i> → stats,
  DELETE /slots/<i> → stats + kill every open connection of the slot.

Settings are ``HEYP_EGRESS_*`` environment variables (see below); ``HEYP_EGRESS_SECRET`` is required and
``HEYP_EGRESS_DENY_CIDRS`` must list the host's own public addresses on a shared server.
Logs: one JSON line per connection with slot, method, port, verdict, reason class, bytes and duration. The
destination host and its addresses are NOT logged (the privacy policy says capture addresses aren't kept after the
job); ``HEYP_EGRESS_LOG_HOSTS=1`` adds them for debugging on a test bed only. Never paths or query strings.
Stdlib only, so it can be audited in one sitting.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import sys
import time
from urllib.parse import urlsplit

from guard import Policy, resolve


def _int(name: str, default: int, lo: int = 0, hi: int = 1 << 40) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        sys.exit(f"{name} must be an integer, got {raw!r}")
    if not lo <= value <= hi:
        sys.exit(f"{name} must be between {lo} and {hi}, got {value}")
    return value


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        sys.exit(f"{name} must be a number, got {raw!r}")


SECRET = os.environ.get("HEYP_EGRESS_SECRET", "")
BIND = os.environ.get("HEYP_EGRESS_BIND", "0.0.0.0")
DENY_PORT = _int("HEYP_EGRESS_DENY_PORT", 3128, 1, 65535)
CONTROL = _int("HEYP_EGRESS_CONTROL_PORT", 3129, 1, 65535)
BASE_PORT = _int("HEYP_EGRESS_BASE_PORT", 3130, 1, 65000)
SLOTS = _int("HEYP_EGRESS_SLOTS", 4, 1, 64)
MAX_JOB_BYTES = _int("HEYP_EGRESS_MAX_JOB_BYTES", 50 * 1024 * 1024, 1024)
MAX_JOB_CONNS = _int("HEYP_EGRESS_MAX_JOB_CONNS", 300, 1)  # total over the job's life
MAX_OPEN_CONNS = _int("HEYP_EGRESS_MAX_OPEN_CONNS", 512, 1)  # whole proxy
CONNECT_TIMEOUT = _float("HEYP_EGRESS_CONNECT_TIMEOUT", 5)
IDLE_TIMEOUT = _float("HEYP_EGRESS_IDLE_TIMEOUT", 20)
MAX_TUNNEL_SECONDS = _float("HEYP_EGRESS_MAX_TUNNEL_SECONDS", 60)
LOG_HOSTS = os.environ.get("HEYP_EGRESS_LOG_HOSTS", "0").strip().lower() in ("1", "true", "yes", "on")
HEAD_LIMIT = 32 * 1024
HOP_BY_HOP = {"proxy-authorization", "proxy-connection", "connection", "keep-alive", "te", "trailer",
              "upgrade", "proxy-authenticate"}

try:
    policy = Policy.from_env("HEYP_EGRESS_")
except ValueError as e:  # a typo in HEYP_EGRESS_DENY_CIDRS must not silently open the box
    sys.exit(f"heypdf-egress: bad policy setting: {e}")
open_conns = 0


class Slot:
    def __init__(self, idx: int):
        self.idx = idx
        self.writers: set[asyncio.StreamWriter] = set()
        self.reset()

    def reset(self) -> None:
        self.kill()
        self.up = self.down = self.conns = 0
        self.blocked: list[dict] = []
        self.hosts: dict[str, int] = {}
        self.over_budget = False

    def stats(self) -> dict:
        return {"slot": self.idx, "bytes_up": self.up, "bytes_down": self.down, "conns": self.conns,
                "open": len(self.writers), "over_budget": self.over_budget, "blocked": self.blocked[:50],
                "hosts": dict(list(self.hosts.items())[:100])}

    def kill(self) -> None:
        for w in list(self.writers):
            w.transport.abort()
        self.writers.clear()


slots = [Slot(i) for i in range(SLOTS)]


def log(**kv) -> None:
    kv["ts"] = round(time.time(), 3)
    print(json.dumps(kv, separators=(",", ":")), flush=True)


def dest(**kv) -> dict:
    """Destination fields (host, ip, ips) for a log line: empty unless HEYP_EGRESS_LOG_HOSTS is on."""
    return kv if LOG_HOSTS else {}


async def reply(w: asyncio.StreamWriter, status: str, reason: str = "") -> None:
    body = f"heypdf egress: {status} {reason}\n".encode()
    w.write(f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
            f"X-Heypdf-Egress: blocked; reason={reason}\r\nConnection: close\r\n\r\n".encode() + body)
    try:
        await asyncio.wait_for(w.drain(), 2)
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        w.close()


def split_host_port(authority: str, default: int) -> tuple[str, int]:
    if authority.startswith("["):
        host, _, rest = authority[1:].partition("]")
        return host, int(rest[1:]) if rest[1:].isdigit() else default
    host, sep, port = authority.rpartition(":")
    if sep and port.isdigit() and ":" not in host:
        return host, int(port)
    return authority, default


async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter, slot: Slot, upstream: bool) -> None:
    try:
        while True:
            chunk = await asyncio.wait_for(src.read(65536), IDLE_TIMEOUT)
            if not chunk:
                break
            if upstream:
                slot.up += len(chunk)
            else:
                slot.down += len(chunk)
            if slot.up + slot.down > MAX_JOB_BYTES:
                slot.over_budget = True
                slot.kill()
                break
            dst.write(chunk)
            await dst.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        try:
            dst.write_eof() if dst.can_write_eof() else dst.close()
        except Exception:
            dst.close()


async def handle(cr: asyncio.StreamReader, cw: asyncio.StreamWriter, slot: Slot | None) -> None:
    global open_conns
    open_conns += 1
    t0 = time.monotonic()
    try:
        try:
            head = await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), 10)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
            return
        if slot is None:
            return await reply(cw, "403 Forbidden", "no-job")
        if open_conns > MAX_OPEN_CONNS:
            return await reply(cw, "503 Service Unavailable", "proxy-busy")
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ")
        except ValueError:
            return await reply(cw, "400 Bad Request", "bad-request-line")
        raw_headers = [(k.strip(), v.strip()) for k, _, v in (ln.partition(":") for ln in lines[1:] if ":" in ln)]
        slot.conns += 1
        if slot.conns > MAX_JOB_CONNS:
            return await reply(cw, "429 Too Many Requests", "job-conn-limit")
        if slot.over_budget:
            return await reply(cw, "413 Payload Too Large", "job-byte-budget")

        if method == "CONNECT":
            host, port = split_host_port(target, 443)
            path_line = None
        else:
            u = urlsplit(target)
            if u.scheme != "http" or not u.hostname:
                return await reply(cw, "400 Bad Request", "scheme")
            try:
                host, port = u.hostname, u.port or 80
            except ValueError:
                return await reply(cw, "400 Bad Request", "port")
            path_line = f"{method} {(u.path or '/') + ('?' + u.query if u.query else '')} {version}\r\n"

        reason = policy.check_name(host) or policy.check_port(port)
        addrs = []
        if not reason:
            try:
                addrs = await asyncio.wait_for(resolve(asyncio.get_running_loop(), host, port), 5)
            except (OSError, asyncio.TimeoutError, UnicodeError, ValueError):
                reason = "dns-failure"
        if not reason and not addrs:
            reason = "dns-empty"
        # Fail closed: one bad record poisons the whole name (no "pick the public one" games).
        for a in addrs:
            reason = reason or policy.check_ip(a)
        if reason:
            ips = [str(a) for a in addrs[:4]]
            slot.blocked.append({"host": host[:253], "port": port, "reason": reason, "ips": ips})
            log(slot=slot.idx, m=method, port=port, verdict="block", reason=reason, **dest(host=host[:253], ips=ips))
            return await reply(cw, "403 Forbidden", reason)

        upstream, last_err, ip = None, "connect-failed", None
        for a in sorted(addrs, key=lambda x: x.version):  # IPv4 first; the vetted IP is what we connect to
            try:
                upstream = await asyncio.wait_for(asyncio.open_connection(str(a), port), CONNECT_TIMEOUT)
                ip = a
                break
            except (OSError, asyncio.TimeoutError) as e:
                last_err = type(e).__name__
        if upstream is None:
            log(slot=slot.idx, m=method, port=port, verdict="error", reason=last_err, **dest(host=host[:253]))
            return await reply(cw, "502 Bad Gateway", last_err)
        ur, uw = upstream
        slot.writers.update({cw, uw})
        slot.hosts[host] = slot.hosts.get(host, 0) + 1

        if path_line is None:
            cw.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await cw.drain()
        else:
            out = [path_line] + [f"{k}: {v}\r\n" for k, v in raw_headers if k.lower() not in HOP_BY_HOP]
            out.append("Connection: close\r\n\r\n")  # one request per upstream connection: no cross-host reuse
            data = "".join(out).encode("latin-1")
            slot.up += len(data)
            uw.write(data)
            await uw.drain()
        t_up = asyncio.create_task(pipe(cr, uw, slot, True))
        t_down = asyncio.create_task(pipe(ur, cw, slot, False))
        # Tunnels end when both sides are done; a plain HTTP exchange ends with the response (Chromium would
        # otherwise park the keep-alive client connection on us until the idle timeout).
        await asyncio.wait({t_up, t_down} if path_line is None else {t_down}, timeout=MAX_TUNNEL_SECONDS)
        for t in (t_up, t_down):
            t.cancel()
        log(slot=slot.idx, m=method, port=port, verdict="allow", up=slot.up, down=slot.down,
            ms=round((time.monotonic() - t0) * 1000), **dest(host=host[:253], ip=str(ip)))
        slot.writers.discard(uw)
        uw.close()
    except Exception as e:  # never let one connection kill the proxy
        log(verdict="error", reason=type(e).__name__)
    finally:
        open_conns -= 1
        if slot:
            slot.writers.discard(cw)
        cw.close()


async def control(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
    try:
        head = (await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), 5)).decode("latin-1").split("\r\n")
        method, path, _ = head[0].split(" ")
        hdrs = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in head[1:] if h)}
        idx = path[7:]
        if method == "GET" and path == "/healthz":
            status, body = "200 OK", {"status": "ok"}
        elif not hmac.compare_digest(hdrs.get("x-egress-secret", "").encode(), SECRET.encode()):
            status, body = "403 Forbidden", {"error": "secret"}
        elif path.startswith("/slots/") and idx.isdigit() and int(idx) < SLOTS and method in ("GET", "POST", "DELETE"):
            slot = slots[int(idx)]
            if method == "POST":
                slot.reset()
            body, status = slot.stats(), "200 OK"
            if method == "DELETE":
                slot.kill()
        elif path == "/health":
            status, body = "200 OK", {"ok": True, "open": open_conns, "slots": SLOTS}
        else:
            status, body = "404 Not Found", {"error": "path"}
        data = json.dumps(body).encode()
        cw.write(f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\n"
                 f"Connection: close\r\n\r\n".encode() + data)
        await cw.drain()
    except Exception:
        pass
    finally:
        cw.close()


async def start(bind: str = BIND) -> list[asyncio.base_events.Server]:
    """Open the deny port, the control port and one port per slot."""
    servers = [await asyncio.start_server(lambda r, w: handle(r, w, None), bind, DENY_PORT, limit=HEAD_LIMIT),
               await asyncio.start_server(control, bind, CONTROL, limit=HEAD_LIMIT)]
    for s in slots:
        servers.append(await asyncio.start_server(lambda r, w, s=s: handle(r, w, s), bind, BASE_PORT + s.idx,
                                                  limit=HEAD_LIMIT))
    log(event="listening", deny=DENY_PORT, control=CONTROL, slots=f"{BASE_PORT}-{BASE_PORT + SLOTS - 1}",
        policy_enabled=policy.enabled, allow_ports=sorted(policy.allow_ports),
        extra_deny=[str(n) for n in policy.extra_deny], log_hosts=LOG_HOSTS)
    return servers


async def main() -> None:
    if len(SECRET) < 16:
        sys.exit("heypdf-egress: HEYP_EGRESS_SECRET must be set (at least 16 characters; openssl rand -hex 32)")
    if not policy.enabled:
        log(event="WARNING", detail="HEYP_EGRESS_POLICY=off: the egress policy is DISABLED (test bed only)")
    servers = await start()
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
