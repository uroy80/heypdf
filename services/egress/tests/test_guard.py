"""Unit tests for the IP/name/port policy (no network)."""
import asyncio

import pytest
from guard import Policy, literal_ip, parse_cidrs, parse_ports, resolve

P = Policy(deny_cidrs="203.0.114.7/32", deny_local_nets=False)

BLOCKED = {
    "127.0.0.1": "loopback", "127.255.255.254": "loopback", "0.0.0.0": "this-network", "10.1.2.3": "private",
    "172.16.0.1": "private", "172.31.255.255": "private", "192.168.1.1": "private", "100.64.0.1": "cgnat",
    "100.127.255.254": "cgnat", "169.254.169.254": "link-local", "169.254.170.2": "link-local",
    "224.0.0.251": "multicast", "239.255.255.250": "multicast", "255.255.255.255": "reserved", "240.0.0.1": "reserved",
    "192.0.0.170": "ietf-protocol", "192.0.2.1": "documentation", "198.18.0.1": "benchmarking",
    "198.51.100.1": "documentation", "203.0.113.9": "documentation", "192.88.99.1": "6to4-relay",
    "::": "unspecified", "::1": "loopback", "::ffff:127.0.0.1": "loopback", "::ffff:169.254.169.254": "link-local",
    "::ffff:10.0.0.1": "private", "::127.0.0.1": "ipv4-compatible", "64:ff9b::a9fe:a9fe": "nat64",
    "64:ff9b:1::1": "nat64-local", "2001:0:4136:e378::1": "ietf-protocol", "2002:a9fe:a9fe::1": "6to4",
    "2001:db8::1": "documentation", "3fff::1": "documentation", "fd00:ec2::254": "unique-local",
    "fc00::1": "unique-local", "fe80::1": "link-local", "fec0::1": "site-local", "ff02::1": "multicast",
    "100::1": "discard", "::ffff:1.1.1.1": "ipv4-mapped", "5f00::1": "srv6", "203.0.114.7": "denied-cidr",
    "4000::1": "not-global-unicast", "fe80::1%eth0": "scoped-ipv6",
}
ALLOWED = ["1.1.1.1", "8.8.8.8", "93.184.215.14", "11.11.11.10", "2606:4700:4700::1111", "2a00:1450:4001:80b::200e",
           "100.63.255.255", "100.128.0.1", "172.15.255.255", "172.32.0.1", "192.169.0.1"]


@pytest.mark.parametrize("ip,reason", sorted(BLOCKED.items()))
def test_blocked(ip, reason):
    assert P.check_ip(ip) == reason


@pytest.mark.parametrize("ip", ALLOWED)
def test_allowed(ip):
    assert P.check_ip(ip) is None


@pytest.mark.parametrize("name", ["localhost", "LOCALHOST.", "a.localhost", "printer.local", "metadata.google.internal",
                                  "host.docker.internal", "router.home.arpa", "metadata", "x.corp", "nas.lan"])
def test_internal_names(name):
    assert P.check_name(name) == "internal-name"


def test_empty_name():
    assert P.check_name(".") == "empty-host"


def test_ports():
    assert P.check_port(443) is None and P.check_port(80) is None
    assert P.check_port(22) == P.check_port(6379) == P.check_port(8080) == "port-not-allowed"


def test_disabled_policy_allows_everything():
    off = Policy(enabled=False, deny_local_nets=False)
    assert off.check_ip("127.0.0.1") is None and off.check_name("localhost") is None and off.check_port(22) is None


def test_bad_configuration_is_an_error():
    with pytest.raises(ValueError):
        parse_cidrs("203.0.113.7/32, not-an-ip")
    with pytest.raises(ValueError):
        parse_ports("80,99999")
    with pytest.raises(ValueError):
        parse_ports("")


def test_from_env(monkeypatch):
    monkeypatch.setenv("X_DENY_CIDRS", "8.8.8.8/32")
    monkeypatch.setenv("X_ALLOW_PORTS", "443")
    monkeypatch.setenv("X_DENY_LOCAL_NETS", "0")
    p = Policy.from_env("X_")
    assert p.check_ip("8.8.8.8") == "denied-cidr" and p.check_port(80) == "port-not-allowed" and p.enabled


def test_literal_ip():
    assert str(literal_ip("[::1]")) == "::1" and literal_ip("example.com") is None and str(literal_ip("1.2.3.4"))


@pytest.mark.parametrize("host", ["0x7f.1", "2130706433", "017700000001", "127.1", "0x7f000001", "[::ffff:7f00:1]",
                                  "[0:0:0:0:0:ffff:127.0.0.1]"])
def test_weird_literals_resolve_to_loopback(host):
    """glibc inet_aton accepts these; we only ever judge the resolved numeric address."""
    try:
        addrs = asyncio.run(_resolve(host.strip("[]")))
    except OSError:
        pytest.skip("resolver unavailable for " + host)
    assert addrs and all(P.check_ip(a) for a in addrs), addrs


async def _resolve(host):
    return await resolve(asyncio.get_running_loop(), host, 80)
