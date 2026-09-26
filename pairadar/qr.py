"""QR codes for the Pages site, in the standard library.

A poster shared as an image into WeChat, Xiaohongshu or LINE loses its links, so the
code on it is the way back to the page. Byte mode at error-correction level M, the
smallest version from 1 to 10 that fits: up to 213 bytes, which covers every URL the
site shares. This follows ISO/IEC 18004 as laid out in Project Nayuki's reference
implementation; tests/test_qr.py checks the fixed parts of the symbol.
"""
from __future__ import annotations

import html

# Level M, by version (index 0 unused).
ECC_PER_BLOCK = (0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26)
BLOCKS = (0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5)
MAX_VERSION = len(BLOCKS) - 1
FORMAT_M = 0  # the two format bits for level M


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _raw_modules(version: int) -> int:
    """Modules left for data and error correction once the fixed patterns are drawn."""
    result = (16 * version + 128) * version + 64
    if version >= 2:
        aligns = version // 7 + 2
        result -= (25 * aligns - 10) * aligns - 55
        if version >= 7:
            result -= 36
    return result


def _data_codewords(version: int) -> int:
    return _raw_modules(version) // 8 - ECC_PER_BLOCK[version] * BLOCKS[version]


def _alignment(version: int) -> list[int]:
    if version == 1:
        return []
    size = version * 4 + 17
    aligns = version // 7 + 2
    step = -(-(size - 13) // (aligns * 2 - 2)) * 2
    positions = [size - 7 - i * step for i in range(aligns - 1)]
    return [6] + positions[::-1]


def _encode(payload: bytes) -> tuple[int, list[int]]:
    for version in range(1, MAX_VERSION + 1):
        count_bits = 8 if version < 10 else 16
        capacity = _data_codewords(version) * 8
        if 4 + count_bits + 8 * len(payload) <= capacity:
            break
    else:
        raise ValueError(f"{len(payload)} bytes do not fit a version {MAX_VERSION} QR code")
    bits = [0, 1, 0, 0] + [(len(payload) >> i) & 1 for i in reversed(range(count_bits))]
    for byte in payload:
        bits += [(byte >> i) & 1 for i in reversed(range(8))]
    bits += [0] * min(4, capacity - len(bits))
    bits += [0] * (-len(bits) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < capacity // 8:
        data.append(pad)
        pad ^= 0xEC ^ 0x11
    return version, data


def _interleave(version: int, data: list[int]) -> list[int]:
    blocks, ecc = BLOCKS[version], ECC_PER_BLOCK[version]
    raw = _raw_modules(version) // 8
    short_blocks = blocks - raw % blocks
    short_len = raw // blocks
    divisor = _rs_divisor(ecc)
    out, k = [], 0
    for i in range(blocks):
        chunk = data[k:k + short_len - ecc + (0 if i < short_blocks else 1)]
        k += len(chunk)
        tail = _rs_remainder(chunk, divisor)
        if i < short_blocks:
            chunk = chunk + [0]
        out.append(chunk + tail)
    return [block[i] for i in range(len(out[0])) for j, block in enumerate(out)
            if i != short_len - ecc or j >= short_blocks]


MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


class _Symbol:
    def __init__(self, version: int) -> None:
        self.version = version
        self.size = version * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]

    def put(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = dark
        self.fixed[y][x] = True

    def patterns(self) -> None:
        size = self.size
        for i in range(size):
            self.put(6, i, i % 2 == 0)
            self.put(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    if 0 <= cx + dx < size and 0 <= cy + dy < size:
                        self.put(cx + dx, cy + dy, max(abs(dx), abs(dy)) not in (2, 4))
        spots = _alignment(self.version)
        last = len(spots) - 1
        for i, cx in enumerate(spots):
            for j, cy in enumerate(spots):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.put(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
        self.format_bits(0)  # reserve the format area before the data goes in
        if self.version >= 7:
            rem = self.version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.version << 12 | rem
            for i in range(18):
                dark = (bits >> i) & 1 == 1
                a, b = size - 11 + i % 3, i // 3
                self.put(a, b, dark)
                self.put(b, a, dark)

    def format_bits(self, mask: int) -> None:
        data = FORMAT_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = [(bits >> i) & 1 == 1 for i in range(15)]
        size = self.size
        for i in range(6):
            self.put(8, i, bit[i])
        self.put(8, 7, bit[6])
        self.put(8, 8, bit[7])
        self.put(7, 8, bit[8])
        for i in range(9, 15):
            self.put(14 - i, 8, bit[i])
        for i in range(8):
            self.put(size - 1 - i, 8, bit[i])
        for i in range(8, 15):
            self.put(8, size - 15 + i, bit[i])
        self.put(8, size - 8, True)

    def codewords(self, data: list[int]) -> None:
        size, i = self.size, 0
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    y = size - 1 - vert if (right + 1) & 2 == 0 else vert
                    if not self.fixed[y][x] and i < len(data) * 8:
                        self.dark[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def masked(self, mask: int) -> list[list[bool]]:
        rule = MASKS[mask]
        return [[dark != (not self.fixed[y][x] and rule(x, y)) for x, dark in enumerate(row)]
                for y, row in enumerate(self.dark)]


def _penalty(grid: list[list[bool]]) -> int:
    """The four scoring rules that pick the mask a scanner reads most easily."""
    size = len(grid)
    lines = ["".join("1" if dark else "0" for dark in row) for row in grid]
    lines += ["".join(lines[y][x] for y in range(size)) for x in range(size)]
    score = 0
    for line in lines:
        run, prev = 0, ""
        for cell in line + "x":
            if cell == prev:
                run += 1
                continue
            if run >= 5:
                score += run - 2
            run, prev = 1, cell
        padded = "0000" + line + "0000"
        for finder in ("10111010000", "00001011101"):
            score += 40 * sum(padded.startswith(finder, i) for i in range(len(padded)))
    for y in range(size - 1):
        for x in range(size - 1):
            if grid[y][x] == grid[y][x + 1] == grid[y + 1][x] == grid[y + 1][x + 1]:
                score += 3
    dark = sum(map(sum, grid))
    score += 10 * (abs(dark * 20 - size * size * 10) // (size * size))
    return score


def matrix(text: str, mask: int | None = None) -> list[list[bool]]:
    """The symbol for `text` as rows of dark (True) and light modules, no quiet zone."""
    version, data = _encode(text.encode("utf-8"))
    symbol = _Symbol(version)
    symbol.patterns()
    symbol.codewords(_interleave(version, data))
    best = None
    for candidate in range(8) if mask is None else (mask,):
        symbol.format_bits(candidate)
        grid = symbol.masked(candidate)
        score = _penalty(grid) if mask is None else 0
        if best is None or score < best[0]:
            best = (score, candidate, grid)
    assert best is not None
    symbol.format_bits(best[1])
    return symbol.masked(best[1])


def rows(text: str) -> list[str]:
    """The symbol as strings of 1 and 0, for a script to draw."""
    return ["".join("1" if dark else "0" for dark in row) for row in matrix(text)]


def svg(text: str, label: str = "QR code") -> str:
    """The symbol as one path, with a four-module quiet zone, dark on light."""
    grid = matrix(text)
    size = len(grid) + 8
    path = "".join(f"M{x + 4},{y + 4}h1v1h-1z" for y, row in enumerate(grid) for x, dark in enumerate(row) if dark)
    return (f'<svg class="qr" viewBox="0 0 {size} {size}" role="img" aria-label="{html.escape(label, quote=True)}" '
            f'shape-rendering="crispEdges"><rect width="{size}" height="{size}" fill="#fff"/>'
            f'<path d="{path}" fill="#05070d"/></svg>')
