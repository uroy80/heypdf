"""Egress policy for a publicly reachable URL renderer: which destinations may be connected to.

The ONLY input that matters for security is the numeric IP the proxy is about to connect() to. Host strings are
never trusted (parser differentials: "0x7f.1", "2130706433", "[::ffff:127.0.0.1]", "127.0.0.1.nip.io", IDN
full-width digits, trailing dots ...); they are resolved first and every resolved address is checked.

Stdlib only. The same file ships in heypdf-egress (enforcement) and heypdf-renderer (fast pre-flight for friendly
errors); tests/test_tools_render.py keeps the two copies identical.
"""
from __future__ import annotations

import ipaddress
import os
import socket

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# IANA special-purpose registries (https://www.iana.org/assignments/iana-ipv4-special-registry,
# https://www.iana.org/assignments/iana-ipv6-special-registry) + cloud/container specifics. Checked IN ADDITION
# to `not ip.is_global`, so a stale Python table cannot open a hole.
_DENY_V4 = {
    "0.0.0.0/8": "this-network",
    "10.0.0.0/8": "private",
    "100.64.0.0/10": "cgnat",  # RFC 6598 shared space; also Tailscale / carrier NAT
    "127.0.0.0/8": "loopback",
    "169.254.0.0/16": "link-local",  # cloud metadata 169.254.169.254, ECS 169.254.170.2, ...
    "172.16.0.0/12": "private",  # Docker default bridges live here
    "192.0.0.0/24": "ietf-protocol",
    "192.0.2.0/24": "documentation",
    "192.31.196.0/24": "as112",
    "192.52.193.0/24": "amt",
    "192.88.99.0/24": "6to4-relay",
    "192.168.0.0/16": "private",
    "192.175.48.0/24": "as112",
    "198.18.0.0/15": "benchmarking",
    "198.51.100.0/24": "documentation",
    "203.0.113.0/24": "documentation",
    "224.0.0.0/4": "multicast",
    "240.0.0.0/4": "reserved",  # includes 255.255.255.255
}
_DENY_V6 = {
    "::/128": "unspecified",
    "::1/128": "loopback",
    "::ffff:0:0/96": "ipv4-mapped",  # unwrapped and re-checked first; deny the raw form anyway
    "::/96": "ipv4-compatible",
    "64:ff9b::/96": "nat64",  # a NAT64 gateway would translate 64:ff9b::a9fe:a9fe -> 169.254.169.254
    "64:ff9b:1::/48": "nat64-local",
    "100::/64": "discard",
    "2001::/23": "ietf-protocol",  # includes Teredo 2001::/32 (embeds an IPv4 address)
    "2001:db8::/32": "documentation",
    "2002::/16": "6to4",  # embeds an IPv4 address
    "3fff::/20": "documentation",
    "5f00::/16": "srv6",
    "fc00::/7": "unique-local",  # AWS IMDSv2 over IPv6 is fd00:ec2::254
    "fe80::/10": "link-local",
    "fec0::/10": "site-local",
    "ff00::/8": "multicast",
}
_DENY: list[tuple[IPNetwork, str]] = [(ipaddress.ip_network(n), r) for n, r in (_DENY_V4 | _DENY_V6).items()]
_GLOBAL_UNICAST_V6 = ipaddress.ip_network("2000::/3")

_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".localdomain", ".intranet", ".corp", ".lan")
_BLOCKED_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "metadata",
                  "metadata.google.internal", "instance-data", "host.docker.internal", "gateway.docker.internal"}


def parse_cidrs(value: str) -> list[IPNetwork]:
    """``"203.0.113.7/32, 2001:db8::7"`` → networks; a bad entry is a configuration error (raises ValueError)."""
    return [ipaddress.ip_network(x.strip(), strict=False) for x in value.split(",") if x.strip()]


def parse_ports(value: str) -> set[int]:
    ports = {int(p) for p in value.split(",") if p.strip()}
    if not ports or any(not 1 <= p <= 65535 for p in ports):
        raise ValueError(f"bad port list {value!r}")
    return ports


def local_networks() -> list[IPNetwork]:
    """Subnets this container is directly attached to (docker bridges, even if a non-RFC1918 pool is used)."""
    nets: list[IPNetwork] = []
    try:
        with open("/proc/net/route") as f:
            next(f)
            for line in f:
                p = line.split()
                dest, mask = int(p[1], 16), int(p[7], 16)
                if dest and mask:  # skip the default route
                    nets.append(ipaddress.ip_network(
                        (socket.inet_ntoa(dest.to_bytes(4, "little")), socket.inet_ntoa(mask.to_bytes(4, "little"))),
                        strict=False))
    except (OSError, StopIteration, ValueError, IndexError):
        pass
    try:
        with open("/proc/net/if_inet6") as f:
            for line in f:
                hexaddr, _idx, plen = line.split()[:3]
                addr = ipaddress.IPv6Address(int(hexaddr, 16))
                nets.append(ipaddress.ip_network(f"{addr}/{int(plen, 16)}", strict=False))
    except (OSError, ValueError):
        pass
    return nets


class Policy:
    def __init__(self, deny_cidrs: str = "", allow_ports: str = "80,443", deny_local_nets: bool = True,
                 enabled: bool = True):
        self.extra_deny = parse_cidrs(deny_cidrs)
        if deny_local_nets:
            self.extra_deny += [n for n in local_networks() if n.prefixlen > 0]
        self.allow_ports = parse_ports(allow_ports)
        self.enabled = enabled

    @classmethod
    def from_env(cls, prefix: str = "HEYP_EGRESS_") -> Policy:
        env = os.environ.get
        return cls(deny_cidrs=env(prefix + "DENY_CIDRS", ""),
                   allow_ports=env(prefix + "ALLOW_PORTS", "80,443") or "80,443",
                   deny_local_nets=env(prefix + "DENY_LOCAL_NETS", "1") == "1",
                   # Positive-control switch for the SSRF test bed ONLY (proves the targets are reachable at all).
                   enabled=env(prefix + "POLICY", "enforce") != "off")

    def check_ip(self, ip: IPAddress | str) -> str | None:
        """None if `ip` may be connected to, else a short reason."""
        if not self.enabled:
            return None
        ip = ipaddress.ip_address(ip)
        if isinstance(ip, ipaddress.IPv6Address):
            if ip.ipv4_mapped is not None:
                return self.check_ip(ip.ipv4_mapped) or "ipv4-mapped"
            if ip.scope_id:
                return "scoped-ipv6"
        for net, reason in _DENY:
            if ip.version == net.version and ip in net:
                return reason
        for net in self.extra_deny:
            if ip.version == net.version and ip in net:
                return "denied-cidr"
        if isinstance(ip, ipaddress.IPv6Address) and ip not in _GLOBAL_UNICAST_V6:
            return "not-global-unicast"
        if not ip.is_global:
            return "not-global"
        return None

    def check_name(self, host: str) -> str | None:
        if not self.enabled:
            return None
        h = host.rstrip(".").lower()
        if not h:
            return "empty-host"
        if h in _BLOCKED_NAMES or h.endswith(_BLOCKED_SUFFIXES):
            return "internal-name"
        return None

    def check_port(self, port: int) -> str | None:
        if not self.enabled:
            return None
        return None if port in self.allow_ports else "port-not-allowed"


def literal_ip(host: str) -> IPAddress | None:
    h = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        return None


async def resolve(loop, host: str, port: int) -> list[IPAddress]:
    """All addresses for host (A + AAAA). An IP literal is returned as-is (never re-parsed by inet_aton)."""
    ip = literal_ip(host)
    if ip is not None:
        return [ip]
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    out: list[IPAddress] = []
    for _fam, _t, _p, _c, sa in infos:
        a = ipaddress.ip_address(sa[0].split("%")[0])
        if a not in out:
            out.append(a)
    return out
