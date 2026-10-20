"""Abuse limits for a public deployment (PLAN.md §2.6). On in hosted mode, off locally.

- **Client key** = SHA-256(daily salt ‖ client IP). The salt is random, lives only in memory and rotates
  at 00:00 UTC, so keys can't be linked across days or reversed from a dump of the counters.
- **Client IP** comes from the socket peer; ``X-Forwarded-For`` is honoured only when the peer is one of
  ``HEYP_TRUSTED_PROXIES`` (and then only up to the first untrusted hop, read right to left).
- **Token buckets** per key: upload bytes/hour, jobs/minute, renders/minute (+ per destination host),
  AI requests/day. A refusal is :class:`RateLimited` (429 + ``Retry-After``).
- Counters are in memory only (they reset on restart). The AI *spend* ledger is persistent and belongs
  to ``app/ai/budget.py`` (it lives in ``HEYP_STATE_DIR``).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import math
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.config import Settings
from app.core.errors import RateLimited

MAX_BUCKETS = 200_000  # bounded memory: least recently used keys are forgotten first
HOUR = 3600.0
DAY = 86400.0


@dataclass(frozen=True)
class ClientInfo:
    """Who is asking, as far as limits are concerned. ``key`` is the salted hash, never the IP."""

    key: str


# ------------------------------------------------------------------ client IP
def _networks(trusted: Iterable[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    nets = []
    for item in trusted:
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return nets


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    value = value.strip().strip('"')
    if value.startswith("[") and "]" in value:  # [v6]:port
        value = value[1:value.index("]")]
    elif value.count(":") == 1:  # v4:port
        value = value.split(":", 1)[0]
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def client_ip(peer: str | None, forwarded_for: str | None, trusted: Iterable[str]) -> str:
    """The client address for limits: the peer, or — when the peer is a trusted proxy — the right-most
    ``X-Forwarded-For`` entry that isn't itself trusted. Malformed entries stop the walk (fail closed:
    the last trusted hop is used, never a value the client could have chosen)."""
    nets = _networks(trusted)
    ip = _parse_ip(peer or "")
    if ip is None:
        return (peer or "unknown")[:64]

    def is_trusted(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return any(addr.version == n.version and addr in n for n in nets)

    if not forwarded_for or not is_trusted(ip):
        return str(ip)
    current = ip
    for hop in reversed([h for h in forwarded_for.split(",") if h.strip()]):
        parsed = _parse_ip(hop)
        if parsed is None:
            break
        current = parsed
        if not is_trusted(parsed):
            break
    return str(current)


def request_ip(request: object, settings: Settings) -> str:
    """``client_ip`` for a Starlette request (or anything with ``.client`` and ``.headers``)."""
    client = getattr(request, "client", None)
    peer = getattr(client, "host", None) if client is not None else None
    headers: Mapping[str, str] = getattr(request, "headers", {}) or {}
    return client_ip(peer, headers.get("x-forwarded-for"), settings.trusted_proxies)


# ------------------------------------------------------------------ hashing
class _Salt:
    """32 random bytes per UTC day, regenerated lazily; the previous day's salt is discarded."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._day: str | None = None
        self._salt = b""

    def current(self, now: float | None = None) -> bytes:
        day = dt.datetime.fromtimestamp(time.time() if now is None else now, dt.timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            if day != self._day:
                self._day, self._salt = day, os.urandom(32)
            return self._salt


_SALT = _Salt()


def client_key(ip: str, now: float | None = None) -> str:
    return hashlib.sha256(_SALT.current(now) + ip.encode("utf-8", "replace")).hexdigest()


def client_info(request: object, settings: Settings) -> ClientInfo:
    return ClientInfo(client_key(request_ip(request, settings)))


# ------------------------------------------------------------------ token buckets
class TokenBucket:
    """``capacity`` tokens refilled continuously over ``period`` seconds."""

    __slots__ = ("capacity", "rate", "tokens", "stamp")

    def __init__(self, capacity: float, period: float, now: float) -> None:
        self.capacity = float(capacity)
        self.rate = self.capacity / period
        self.tokens = self.capacity
        self.stamp = now

    def _refill(self, now: float) -> None:
        if now > self.stamp:
            self.tokens = min(self.capacity, self.tokens + (now - self.stamp) * self.rate)
            self.stamp = now

    def take(self, amount: float, now: float) -> float:
        """Take ``amount``; returns 0 on success, else the seconds until it would fit (nothing taken).
        A request larger than the whole capacity waits for a full bucket and then drains it."""
        self._refill(now)
        need = min(amount, self.capacity)
        if self.tokens >= need:
            self.tokens -= amount if amount <= self.capacity else self.tokens
            return 0.0
        return (need - self.tokens) / self.rate

    def give_back(self, amount: float) -> None:
        self.tokens = min(self.capacity, self.tokens + amount)


@dataclass(frozen=True)
class Rule:
    capacity: float
    period: float
    message: str


class Limiter:
    """All per-client buckets of one app. ``check(kind, client, amount)`` raises RateLimited."""

    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.limits_enabled
        mb = settings.limit_upload_mb_hour
        self.rules: dict[str, Rule] = {
            "upload_bytes": Rule(mb * 1024 * 1024, HOUR, f"Upload limit reached ({mb} MB per hour)"),
            "jobs": Rule(settings.limit_jobs_minute, 60.0,
                         f"Too many jobs — at most {settings.limit_jobs_minute} per minute"),
            "renders": Rule(settings.limit_renders_minute, 60.0,
                            f"Too many page renders — at most {settings.limit_renders_minute} per minute"),
            "render_host": Rule(settings.limit_render_host_minute, 60.0,
                                "Too many renders of that website — try again in a minute"),
            "ai": Rule(settings.limit_ai_day, DAY,
                       f"Daily AI limit reached ({settings.limit_ai_day} requests per day)"),
        }
        self._buckets: OrderedDict[tuple[str, str], TokenBucket] = OrderedDict()
        self._lock = threading.Lock()

    def check(self, kind: str, client: ClientInfo | None, amount: float = 1.0, *, scope: str = "",
              now: float | None = None) -> None:
        """Take ``amount`` from ``client``'s ``kind`` bucket (``scope`` sub-keys it, e.g. a host name)."""
        if not self.enabled or client is None:
            return
        rule = self.rules[kind]
        if rule.capacity <= 0:
            raise RateLimited(rule.message, int(rule.period))
        now = time.monotonic() if now is None else now
        key = (kind, f"{client.key}|{scope}" if scope else client.key)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = self._buckets[key] = TokenBucket(rule.capacity, rule.period, now)
            else:
                self._buckets.move_to_end(key)
            wait = bucket.take(float(amount), now)
            while len(self._buckets) > MAX_BUCKETS:
                self._buckets.popitem(last=False)
        if wait > 0:
            raise RateLimited(rule.message, int(math.ceil(wait)))

    def refund(self, kind: str, client: ClientInfo | None, amount: float = 1.0, *, scope: str = "") -> None:
        """Give tokens back (e.g. an upload that was refused for another reason)."""
        if not self.enabled or client is None:
            return
        key = (kind, f"{client.key}|{scope}" if scope else client.key)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is not None:
                bucket.give_back(float(amount))

    def check_render(self, client: ClientInfo | None, host: str) -> None:
        """Renderer tools: the per-client rate and the per-(client, destination host) rate."""
        self.check("renders", client)
        try:
            self.check("render_host", client, scope=host.strip().lower()[:255])
        except RateLimited:
            self.refund("renders", client)
            raise

    def __len__(self) -> int:
        with self._lock:
            return len(self._buckets)


def get_limiter(app: object) -> Limiter | None:
    """The app's limiter (``app.state.limiter``), for tool code that only has an app or request."""
    state = getattr(app, "state", None)
    return getattr(state, "limiter", None) if state is not None else None
