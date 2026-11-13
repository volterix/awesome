"""
Minimal QR code generator (byte mode, error-correction level M, versions 1-10).

Enough for UPI payment links (upi://pay?...), which are well under 200 bytes.
Standard library only. Returns a square matrix of booleans (True = dark).
Algorithm follows ISO/IEC 18004.
"""

# version -> (ec codewords per block, [(block count, data codewords per block), ...]) for level M
_BLOCKS_M = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]),
    5: (24, [(2, 43)]), 6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]), 10: (26, [(4, 43), (1, 44)]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
          7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
_ECL_M_BITS = 0  # format bits for level M

# GF(256) arithmetic, primitive polynomial 0x11D
_EXP, _LOG = [0] * 512, [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _mul(root, 2)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _mul(coef, factor)
    return result


def _data_capacity(version):
    return sum(n * k for n, k in _BLOCKS_M[version][1])


def _encode_codewords(data, version):
    bits = []

    def put(val, n):
        bits.extend((val >> i) & 1 for i in range(n - 1, -1, -1))

    put(0b0100, 4)                                   # byte mode
    put(len(data), 8 if version < 10 else 16)
    for b in data:
        put(b, 8)
    cap = _data_capacity(version) * 8
    put(0, min(4, cap - len(bits)))                  # terminator
    put(0, (-len(bits)) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < _data_capacity(version):
        words.append(pad)
        pad ^= 0xEC ^ 0x11
    # split into blocks, add error correction, interleave
    ec_len, groups = _BLOCKS_M[version]
    divisor = _rs_divisor(ec_len)
    blocks, pos = [], 0
    for count, size in groups:
        for _ in range(count):
            blk = words[pos:pos + size]
            pos += size
            blocks.append((blk, _rs_remainder(blk, divisor)))
    out = []
    for i in range(max(len(b[0]) for b in blocks)):
        out += [b[0][i] for b in blocks if i < len(b[0])]
    for i in range(ec_len):
        out += [b[1][i] for b in blocks]
    return out


_MASKS = [
    lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
]


class _Matrix:
    def __init__(self, version):
        self.v = version
        self.size = n = version * 4 + 17
        self.m = [[False] * n for _ in range(n)]
        self.fn = [[False] * n for _ in range(n)]

    def set_fn(self, x, y, dark):
        self.m[y][x] = dark
        self.fn[y][x] = True

    def draw_function_patterns(self):
        n = self.size
        for i in range(n):                          # timing
            self.set_fn(6, i, i % 2 == 0)
            self.set_fn(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):   # finders + separators
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self.set_fn(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _ALIGN[self.v]
        last = len(pos) - 1
        for i, ax in enumerate(pos):                # alignment patterns
            for j, ay in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_fn(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)                         # reserve; real bits drawn after masking
        if self.v >= 7:
            rem = self.v
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.v << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = n - 11 + i % 3, i // 3
                self.set_fn(a, b, bit)
                self.set_fn(b, a, bit)

    def draw_format(self, mask):
        data = _ECL_M_BITS << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        g = lambda i: (bits >> i) & 1 == 1  # noqa: E731
        n = self.size
        for i in range(6):
            self.set_fn(8, i, g(i))
        self.set_fn(8, 7, g(6))
        self.set_fn(8, 8, g(7))
        self.set_fn(7, 8, g(8))
        for i in range(9, 15):
            self.set_fn(14 - i, 8, g(i))
        for i in range(8):
            self.set_fn(n - 1 - i, 8, g(i))
        for i in range(8, 15):
            self.set_fn(8, n - 15 + i, g(i))
        self.set_fn(8, n - 8, True)                 # always-dark module

    def place_data(self, codewords):
        n, i, total = self.size, 0, len(codewords) * 8
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = n - 1 - vert if upward else vert
                    if not self.fn[y][x] and i < total:
                        self.m[y][x] = (codewords[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        f = _MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fn[y][x] and f(x, y):
                    self.m[y][x] = not self.m[y][x]

    def penalty(self):
        n, m, score = self.size, self.m, 0
        lines = [m[y] for y in range(n)] + [[m[y][x] for y in range(n)] for x in range(n)]
        for line in lines:
            run, prev = 0, None
            for c in line:
                if c == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, prev = 1, c
            if run >= 5:
                score += run - 2
            s = "".join("1" if c else "0" for c in line)
            for pat in ("10111010000", "00001011101"):
                start = s.find(pat)
                while start != -1:
                    score += 40
                    start = s.find(pat, start + 1)
        for y in range(n - 1):
            for x in range(n - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(row) for row in m)
        score += abs(dark * 20 - n * n * 10) // (n * n) * 10
        return score


def make(text, mask=None, version=None):
    """Return a QR code for `text` as a list of rows of booleans (no quiet zone)."""
    data = text.encode("utf-8")
    if version is None:
        version = next((v for v in range(1, 11)
                        if 4 + (8 if v < 10 else 16) + 8 * len(data) <= _data_capacity(v) * 8), None)
        if version is None:
            raise ValueError("Text too long for this QR generator (max ~210 bytes)")
    codewords = _encode_codewords(data, version)
    best = None
    for mk in ([mask] if mask is not None else range(8)):
        q = _Matrix(version)
        q.draw_function_patterns()
        q.place_data(codewords)
        q.apply_mask(mk)
        q.draw_format(mk)
        p = q.penalty()
        if best is None or p < best[0]:
            best = (p, q)
    return best[1].m


def upi_link(vpa, name, amount, note):
    """Build a UPI deep link understood by GPay, PhonePe, Paytm, BHIM and bank apps."""
    from urllib.parse import quote
    parts = [("pa", vpa), ("pn", name), ("am", f"{amount:.2f}"), ("cu", "INR"), ("tn", note)]
    return "upi://pay?" + "&".join(f"{k}={quote(str(v), safe='@.-_')}" for k, v in parts)
