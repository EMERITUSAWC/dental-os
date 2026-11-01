"""A small QR code maker, so the two-step login setup screen can show a code to scan without any extra package.

It writes byte-mode codes at error-correction level M, versions 1 to 10 (up to 213 bytes). That is plenty for a login setup
link. It is written from the published QR standard (ISO/IEC 18004) and the tests scan its output back with a real decoder."""

# Per version at level M: (error-correction codewords per block, [(number of blocks, data codewords per block), ...]).
_M = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]), 5: (24, [(2, 43)]),
    6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]), 9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
          9: [6, 26, 46], 10: [6, 28, 50]}
_FORMAT_M = 0          # the two format bits that mean "level M"


class QRError(ValueError):
    pass


# ---- Reed-Solomon over GF(256), polynomial 0x11D
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


def _mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _generator(n):
    poly = [1]
    for i in range(n):
        nxt = [0] * (len(poly) + 1)
        for j, c in enumerate(poly):
            nxt[j] ^= c
            nxt[j + 1] ^= _mul(c, _EXP[i])
        poly = nxt
    return poly


def _remainder(data, n):
    gen = _generator(n)
    rem = list(data) + [0] * n
    for i in range(len(data)):
        coef = rem[i]
        if coef:
            for j, g in enumerate(gen):
                rem[i + j] ^= _mul(g, coef)
    return rem[len(data):]


def data_capacity(ver):
    ec, blocks = _M[ver]
    return sum(n * d for n, d in blocks)


def byte_capacity(ver):
    bits = data_capacity(ver) * 8 - 4 - (16 if ver >= 10 else 8)
    return bits // 8


def _blank_function_map(ver):
    """True where a module belongs to a fixed pattern and cannot hold data."""
    size = 17 + 4 * ver
    fm = [[False] * size for _ in range(size)]

    def box(x0, y0, w, h):
        for y in range(max(0, y0), min(size, y0 + h)):
            for x in range(max(0, x0), min(size, x0 + w)):
                fm[y][x] = True

    box(0, 0, 9, 9)                          # top-left finder, its separator and format bits
    box(size - 8, 0, 8, 9)                   # top-right
    box(0, size - 8, 9, 8)                   # bottom-left (this also covers the dark module)
    for i in range(size):
        fm[6][i] = True
        fm[i][6] = True                      # timing lines
    pos = _ALIGN[ver]
    for i, cy in enumerate(pos):
        for j, cx in enumerate(pos):
            if (i == 0 and j == 0) or (i == 0 and j == len(pos) - 1) or (i == len(pos) - 1 and j == 0):
                continue
            box(cx - 2, cy - 2, 5, 5)
    if ver >= 7:
        box(size - 11, 0, 3, 6)
        box(0, size - 11, 6, 3)
    return fm


def _format_bits(mask):
    data = (_FORMAT_M << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def _version_bits(ver):
    rem = ver
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return (ver << 12) | rem


def _codewords(data, ver):
    bits = []

    def put(value, n):
        for i in range(n - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)
    put(len(data), 16 if ver >= 10 else 8)
    for b in data:
        put(b, 8)
    total = data_capacity(ver) * 8
    put(0, min(4, total - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    while len(words) < data_capacity(ver):
        words.append(pad[(len(words) - len(bits) // 8) % 2])
    ec, blocks = _M[ver]
    parts, ecs, at = [], [], 0
    for count, size in blocks:
        for _ in range(count):
            chunk = words[at:at + size]
            at += size
            parts.append(chunk)
            ecs.append(_remainder(chunk, ec))
    out = []
    for i in range(max(len(p) for p in parts)):
        for p in parts:
            if i < len(p):
                out.append(p[i])
    for i in range(ec):
        for e in ecs:
            out.append(e[i])
    return out


_MASKS = [
    lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0, lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0, lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0, lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
]


def _penalty(m):
    size = len(m)
    score = 0
    lines = [row for row in m] + [[m[y][x] for y in range(size)] for x in range(size)]
    for line in lines:
        run = 1
        for i in range(1, size):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + run - 5
                run = 1
        if run >= 5:
            score += 3 + run - 5
        s = "".join("1" if v else "0" for v in line)
        for pat in ("00001011101", "10111010000"):
            at = s.find(pat)
            while at != -1:
                score += 40
                at = s.find(pat, at + 1)
    for y in range(size - 1):
        for x in range(size - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                score += 3
    dark = sum(sum(1 for v in r if v) for r in m)
    score += (abs(dark * 20 - size * size * 10) // (size * size)) * 10
    return score


def _draw_function(m, ver):
    size = len(m)

    def setm(x, y, v):
        if 0 <= x < size and 0 <= y < size:
            m[y][x] = v

    for i in range(size):                                           # timing lines first, the finders are drawn over their ends
        setm(6, i, i % 2 == 0)
        setm(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):          # finder patterns with a white ring around them
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                d = max(abs(dx), abs(dy))
                setm(cx + dx, cy + dy, d not in (2, 4))
    pos = _ALIGN[ver]
    for i, cy in enumerate(pos):
        for j, cx in enumerate(pos):
            if (i == 0 and j == 0) or (i == 0 and j == len(pos) - 1) or (i == len(pos) - 1 and j == 0):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    setm(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
    if ver >= 7:
        bits = _version_bits(ver)
        for i in range(18):
            bit = (bits >> i) & 1 == 1
            a, b = size - 11 + i % 3, i // 3
            setm(a, b, bit)
            setm(b, a, bit)


def _draw_format(m, mask):
    size = len(m)
    bits = _format_bits(mask)

    def b(i):
        return (bits >> i) & 1 == 1
    for i in range(6):
        m[i][8] = b(i)
    m[7][8] = b(6)
    m[8][8] = b(7)
    m[8][7] = b(8)
    for i in range(9, 15):
        m[8][14 - i] = b(i)
    for i in range(8):
        m[8][size - 1 - i] = b(i)
    for i in range(8, 15):
        m[size - 15 + i][8] = b(i)
    m[size - 8][8] = True                    # the always-dark module


def make(text):
    """The QR code for `text` as a list of rows of booleans (True is a dark module)."""
    data = text.encode("utf-8")
    ver = next((v for v in range(1, 11) if byte_capacity(v) >= len(data)), None)
    if ver is None:
        raise QRError("That is too long for this QR maker.")
    size = 17 + 4 * ver
    fixed = _blank_function_map(ver)
    words = _codewords(data, ver)
    bits = [(w >> (7 - i)) & 1 for w in words for i in range(8)]
    base = [[False] * size for _ in range(size)]
    _draw_function(base, ver)
    k = 0
    for right in range(size - 1, 0, -2):
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                y = size - 1 - vert if ((right + 1) & 2) == 0 else vert
                if not fixed[y][x] and k < len(bits):
                    base[y][x] = bits[k] == 1
                    k += 1
    best, best_score = None, None
    for mask in range(8):
        trial = [row[:] for row in base]
        for y in range(size):
            for x in range(size):
                if not fixed[y][x] and _MASKS[mask](x, y):
                    trial[y][x] = not trial[y][x]
        _draw_format(trial, mask)
        score = _penalty(trial)
        if best is None or score < best_score:
            best, best_score = trial, score
    return best


def svg(text, pixels=220, label="QR code"):
    """The code as an inline SVG: dark modules on a white ground with the standard four-module quiet zone."""
    rows = make(text)
    n = len(rows)
    total = n + 8
    path = []
    for y, row in enumerate(rows):
        x = 0
        while x < n:
            if row[x]:
                start = x
                while x < n and row[x]:
                    x += 1
                path.append(f"M{start + 4} {y + 4}h{x - start}v1h-{x - start}z")
            else:
                x += 1
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {total}" width="{pixels}" height="{pixels}" role="img" '
            f'aria-label="{label}" shape-rendering="crispEdges"><rect width="{total}" height="{total}" fill="#fff"/>'
            f'<path d="{"".join(path)}" fill="#000"/></svg>')
