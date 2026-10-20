"""A small QR code encoder (byte mode, error correction level M, versions 1–10) → SVG.

Used for the "scan with your phone" hand-off link; no third-party QR package is needed. Follows ISO/IEC
18004: Reed–Solomon over GF(256) (polynomial 0x11D), the eight data masks with the standard penalty
rules, BCH-coded format and version information.
"""
from __future__ import annotations

# (data codewords per block, number of blocks) groups and EC codewords per block, level M
_M_TABLE: dict[int, tuple[int, list[tuple[int, int]]]] = {
    1: (10, [(16, 1)]), 2: (16, [(28, 1)]), 3: (26, [(44, 1)]), 4: (18, [(32, 2)]), 5: (24, [(43, 2)]),
    6: (16, [(27, 4)]), 7: (18, [(31, 4)]), 8: (22, [(38, 2), (39, 2)]), 9: (22, [(36, 3), (37, 2)]),
    10: (26, [(43, 4), (44, 1)]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
          9: [6, 26, 46], 10: [6, 28, 50]}
MAX_BYTES = 213  # version 10-M holds 216 data codewords

# GF(256) tables
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(n: int) -> list[int]:
    g = [1]
    for i in range(n):
        nxt = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            nxt[j] ^= c
            nxt[j + 1] ^= _gf_mul(c, _EXP[i])
        g = nxt
    return g


def _rs_remainder(data: list[int], n: int) -> list[int]:
    gen = _rs_generator(n)
    rem = [0] * n
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        for i in range(n):
            rem[i] ^= _gf_mul(gen[i + 1], factor)
    return rem


def _version_for(length: int) -> int:
    for v in range(1, 11):
        _ec, groups = _M_TABLE[v]
        capacity = sum(d * c for d, c in groups)
        header_bits = 4 + (8 if v < 10 else 16)
        if header_bits + 8 * length <= capacity * 8:
            return v
    raise ValueError(f"text too long for a QR code (max {MAX_BYTES} bytes)")


def _codewords(data: bytes, version: int) -> list[int]:
    ec_len, groups = _M_TABLE[version]
    capacity = sum(d * c for d, c in groups)
    bits: list[int] = []

    def put(value: int, n: int) -> None:
        bits.extend((value >> i) & 1 for i in range(n - 1, -1, -1))

    put(0b0100, 4)
    put(len(data), 8 if version < 10 else 16)
    for b in data:
        put(b, 8)
    put(0, min(4, capacity * 8 - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < capacity:
        words.append(pad)
        pad ^= 0xEC ^ 0x11
    blocks, k = [], 0
    for size, count in groups:
        for _ in range(count):
            blocks.append(words[k:k + size])
            k += size
    ecs = [_rs_remainder(b, ec_len) for b in blocks]
    out: list[int] = []
    for i in range(max(len(b) for b in blocks)):
        out.extend(b[i] for b in blocks if i < len(b))
    for i in range(ec_len):
        out.extend(e[i] for e in ecs)
    return out


def _bch(value: int, poly: int, bits: int) -> int:
    deg = poly.bit_length() - 1
    rem = value << deg
    for i in range(bits + deg - 1, deg - 1, -1):
        if rem >> i & 1:
            rem ^= poly << (i - deg)
    return (value << deg) | rem


_MASKS = [
    lambda r, c: (r + c) % 2 == 0, lambda r, c: r % 2 == 0, lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0, lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0, lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


class _Grid:
    def __init__(self, version: int) -> None:
        self.v = version
        self.n = 17 + 4 * version
        self.m = [[False] * self.n for _ in range(self.n)]
        self.fixed = [[False] * self.n for _ in range(self.n)]

    def set(self, r: int, c: int, dark: bool) -> None:
        self.m[r][c] = dark
        self.fixed[r][c] = True

    def functions(self) -> None:
        n = self.n
        for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):
            for dr in range(-1, 8):
                for dc in range(-1, 8):
                    r, c = r0 + dr, c0 + dc
                    if 0 <= r < n and 0 <= c < n:
                        ring = max(abs(dr - 3), abs(dc - 3))
                        self.set(r, c, ring != 2 and ring != 4)
        for i in range(8, n - 8):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        pos = _ALIGN[self.v]
        last = len(pos) - 1
        for i, r in enumerate(pos):
            for j, c in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue  # finder corners
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        self.set(r + dr, c + dc, max(abs(dr), abs(dc)) != 1)
        self.format_bits(0)  # reserve
        if self.v >= 7:
            bits = _bch(self.v, 0x1F25, 6)
            for i in range(18):
                dark = bool(bits >> i & 1)
                a, b = n - 11 + i % 3, i // 3
                self.set(a, b, dark)
                self.set(b, a, dark)

    def format_bits(self, mask: int) -> None:
        n = self.n
        bits = _bch((0b00 << 3) | mask, 0x537, 5) ^ 0x5412  # level M = 00

        def bit(i: int) -> bool:
            return bool(bits >> i & 1)

        for i in range(6):
            self.set(i, 8, bit(i))
        self.set(7, 8, bit(6))
        self.set(8, 8, bit(7))
        self.set(8, 7, bit(8))
        for i in range(9, 15):
            self.set(8, 14 - i, bit(i))
        for i in range(8):
            self.set(8, n - 1 - i, bit(i))
        for i in range(8, 15):
            self.set(n - 15 + i, 8, bit(i))
        self.set(n - 8, 8, True)  # the dark module

    def place(self, words: list[int]) -> None:
        n = self.n
        bits = [(w >> (7 - i)) & 1 for w in words for i in range(8)]
        k = 0
        upward = True
        col = n - 1
        while col > 0:
            if col == 6:
                col -= 1
            rows = range(n - 1, -1, -1) if upward else range(n)
            for r in rows:
                for c in (col, col - 1):
                    if not self.fixed[r][c]:
                        self.m[r][c] = bool(bits[k]) if k < len(bits) else False
                        k += 1
            upward = not upward
            col -= 2

    def masked(self, mask: int) -> list[list[bool]]:
        fn = _MASKS[mask]
        return [[self.m[r][c] ^ (not self.fixed[r][c] and fn(r, c)) for c in range(self.n)] for r in range(self.n)]


def _penalty(m: list[list[bool]]) -> int:
    n = len(m)
    score = 0
    lines = m + [list(col) for col in zip(*m, strict=True)]
    for line in lines:
        run, prev = 0, None
        for v in line:
            if v == prev:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run, prev = 1, v
        if run >= 5:
            score += run - 2
        s = "".join("1" if v else "0" for v in line)
        score += 40 * (s.count("10111010000") + s.count("00001011101"))
    for r in range(n - 1):
        for c in range(n - 1):
            v = m[r][c]
            if v == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    dark = sum(v for row in m for v in row)
    score += 10 * (abs(dark * 20 - n * n * 10) // (n * n))
    return score


def matrix(text: str) -> list[list[bool]]:
    """The QR module matrix (True = dark) for ``text`` (UTF-8, byte mode, level M)."""
    data = text.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError(f"text too long for a QR code (max {MAX_BYTES} bytes)")
    version = _version_for(len(data))
    words = _codewords(data, version)
    best: tuple[int, list[list[bool]]] | None = None
    for mask in range(8):
        grid = _Grid(version)
        grid.functions()
        grid.place(words)
        grid.format_bits(mask)
        m = grid.masked(mask)
        p = _penalty(m)
        if best is None or p < best[0]:
            best = (p, m)
    assert best is not None
    return best[1]


def svg(text: str, *, border: int = 4, dark: str = "#111111", light: str = "#ffffff") -> str:
    """A crisp, scalable SVG of the QR code (one path, ``shape-rendering: crispEdges``)."""
    m = matrix(text)
    n = len(m) + 2 * border
    parts = [f"M{c + border},{r + border}h1v1h-1z" for r, row in enumerate(m) for c, v in enumerate(row) if v]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges" '
            f'role="img" aria-label="QR code"><rect width="{n}" height="{n}" fill="{light}"/>'
            f'<path fill="{dark}" d="{"".join(parts)}"/></svg>')
