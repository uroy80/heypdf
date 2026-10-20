"""heypdf-egress test set-up: the proxy runs in-process on free loopback ports (no network needed).

Run inside the image: ``docker build --target test -t heypdf-egress:test services/egress &&
docker run --rm --network none --read-only heypdf-egress:test``.
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path


def _free_ports(n: int) -> list[int]:
    """n consecutive free ports (the proxy needs BASE..BASE+SLOTS-1 contiguous)."""
    for base in range(23000, 60000, 37):
        socks = []
        try:
            for p in range(base, base + n):
                s = socket.socket()
                s.bind(("127.0.0.1", p))
                socks.append(s)
            return list(range(base, base + n))
        except OSError:
            continue
        finally:
            for s in socks:
                s.close()
    raise RuntimeError("no free ports")


PORTS = _free_ports(5)  # deny, control, slot 0, slot 1, slot 2
os.environ.update({
    "HEYP_EGRESS_SECRET": "test-secret-0123456789",
    "HEYP_EGRESS_BIND": "127.0.0.1",
    "HEYP_EGRESS_DENY_PORT": str(PORTS[0]),
    "HEYP_EGRESS_CONTROL_PORT": str(PORTS[1]),
    "HEYP_EGRESS_BASE_PORT": str(PORTS[2]),
    "HEYP_EGRESS_SLOTS": "3",
    "HEYP_EGRESS_MAX_JOB_BYTES": str(2 * 1024 * 1024),
    "HEYP_EGRESS_MAX_JOB_CONNS": "20",
    "HEYP_EGRESS_DENY_CIDRS": "203.0.114.7/32",
    "HEYP_EGRESS_IDLE_TIMEOUT": "3",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
