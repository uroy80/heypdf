"""app.core.limits: trusted-proxy client IP, salted daily keys and token buckets (hosted mode only)."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from app.config import Settings, parse_trusted_proxies
from app.core import limits
from app.core.errors import RateLimited
from app.core.limits import ClientInfo, Limiter, TokenBucket, client_ip, client_key

PROXIES = ("10.0.0.0/8", "127.0.0.1/32", "::1/128")


# ------------------------------------------------------------------ client IP
def test_direct_clients_are_their_peer_and_cannot_spoof():
    assert client_ip("203.0.113.9", None, PROXIES) == "203.0.113.9"
    # an untrusted peer's X-Forwarded-For is ignored entirely
    assert client_ip("203.0.113.9", "1.2.3.4", PROXIES) == "203.0.113.9"


def test_trusted_proxy_hops_are_walked_right_to_left():
    assert client_ip("10.0.0.2", "198.51.100.7", PROXIES) == "198.51.100.7"
    # client-supplied junk on the left is never reached: the first untrusted hop from the right wins
    assert client_ip("10.0.0.2", "6.6.6.6, 198.51.100.7, 10.0.0.5", PROXIES) == "198.51.100.7"
    assert client_ip("127.0.0.1", "198.51.100.7:4711", PROXIES) == "198.51.100.7"
    assert client_ip("::1", "[2001:db8::1]:443", PROXIES) == "2001:db8::1"


def test_malformed_forwarded_values_fail_closed():
    # garbage stops the walk at the last trusted hop rather than trusting a client-chosen value
    assert client_ip("10.0.0.2", "not-an-ip", PROXIES) == "10.0.0.2"
    assert client_ip("10.0.0.2", "198.51.100.7, bogus", PROXIES) == "10.0.0.2"
    assert client_ip("10.0.0.2", "10.0.0.3, 10.0.0.4", PROXIES) == "10.0.0.3"  # all trusted: the left-most


def test_ipv4_mapped_ipv6_peer_is_normalised():
    assert client_ip("::ffff:203.0.113.9", None, PROXIES) == "203.0.113.9"
    assert client_ip(None, "1.2.3.4", PROXIES) == "unknown"


def test_request_ip_reads_a_starlette_like_request():
    settings = Settings(trusted_proxies=PROXIES)
    req = SimpleNamespace(client=SimpleNamespace(host="10.1.1.1"), headers={"x-forwarded-for": "192.0.2.4"})
    assert limits.request_ip(req, settings) == "192.0.2.4"


def test_trusted_proxy_setting_refuses_catch_alls():
    assert parse_trusted_proxies("10.0.0.0/8, 172.18.0.3 ::1") == ("10.0.0.0/8", "172.18.0.3/32", "::1/128")
    for bad in ("*", "0.0.0.0/0", "::/0", "example.com"):
        with pytest.raises(ValueError):
            parse_trusted_proxies(bad)
    with pytest.raises(ValueError):
        Settings.from_env({"HEYP_TRUSTED_PROXIES": "*"})


# ------------------------------------------------------------------ keys
def test_client_keys_are_salted_per_day():
    day1 = dt.datetime(2026, 10, 18, 12, tzinfo=dt.timezone.utc).timestamp()
    day2 = day1 + 86400
    a = client_key("198.51.100.7", day1)
    assert a == client_key("198.51.100.7", day1 + 60)  # stable within the day
    assert a != client_key("198.51.100.8", day1)
    assert len(a) == 64 and "198" not in a
    b = client_key("198.51.100.7", day2)
    assert b != a  # a new salt every UTC day: keys can't be linked across days
    assert client_key("198.51.100.7", day1) != a  # yesterday's salt is gone for good


# ------------------------------------------------------------------ buckets
def test_token_bucket_refills_continuously():
    bucket = TokenBucket(10, 60.0, now=0.0)
    assert bucket.take(10, 0.0) == 0.0
    assert bucket.take(1, 0.0) == pytest.approx(6.0)  # one token every 6 s
    assert bucket.take(1, 6.0) == 0.0
    assert bucket.take(100, 6.0) > 0  # more than the capacity: waits for a full bucket
    assert bucket.take(100, 66.0) == 0.0 and bucket.tokens == 0.0


def test_limiter_is_off_locally_and_on_when_hosted():
    client = ClientInfo("k" * 64)
    local = Limiter(Settings(limit_jobs_minute=1))
    for _ in range(5):
        local.check("jobs", client)
    hosted = Limiter(Settings(mode="hosted", limit_jobs_minute=2))
    hosted.check("jobs", client, now=0.0)
    hosted.check("jobs", client, now=0.0)
    with pytest.raises(RateLimited) as info:
        hosted.check("jobs", client, now=0.0)
    assert info.value.status == 429 and info.value.retry_after == 30
    hosted.check("jobs", ClientInfo("other"), now=0.0)  # per client
    hosted.refund("jobs", client)
    hosted.check("jobs", client, now=0.0)
    assert Limiter(Settings(limits=True)).enabled and not Limiter(Settings(mode="hosted", limits=False)).enabled


def test_upload_bytes_and_render_hosts():
    client = ClientInfo("c")
    limiter = Limiter(Settings(mode="hosted", limit_upload_mb_hour=1, limit_renders_minute=3,
                               limit_render_host_minute=2))
    limiter.check("upload_bytes", client, 900_000, now=0.0)
    with pytest.raises(RateLimited, match="1 MB per hour"):
        limiter.check("upload_bytes", client, 200_000, now=0.0)
    limiter.check_render(client, "Example.com")
    limiter.check_render(client, "example.com")
    with pytest.raises(RateLimited, match="that website"):
        limiter.check_render(client, "EXAMPLE.com")
    limiter.check_render(client, "other.org")  # the refused render did not use a render token
    with pytest.raises(RateLimited, match="page renders"):
        limiter.check_render(client, "third.net")


def test_disabled_rules_refuse_everything():
    limiter = Limiter(Settings(mode="hosted", limit_ai_day=0))
    with pytest.raises(RateLimited, match="Daily AI limit"):
        limiter.check("ai", ClientInfo("x"))


def test_bucket_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(limits, "MAX_BUCKETS", 5)
    limiter = Limiter(Settings(mode="hosted"))
    for i in range(20):
        limiter.check("jobs", ClientInfo(str(i)))
    assert len(limiter) == 5


def test_get_limiter_from_an_app():
    limiter = Limiter(Settings())
    assert limits.get_limiter(SimpleNamespace(state=SimpleNamespace(limiter=limiter))) is limiter
    assert limits.get_limiter(object()) is None
