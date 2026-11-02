"""A small independent sixel decoder for tests: parses the stream into rows of RGB triples (None = never drawn).

Also the golden-image helpers (SX): PPM read/write, a tolerance compare that catches a single wrong pixel row, and a
stream mutator that flips one pixel row inside the sixel bands. Pure Python, no dependencies, so the other apps can
vendor this file as it is.
"""

import re
from dataclasses import dataclass
from pathlib import Path

Rows = list[list[tuple[int, int, int] | None]]

_TOKEN = re.compile(r'\x1bP([\d;]*)q|"([\d;]*)|#(\d+)(?:;(\d+);(\d+);(\d+);(\d+))?|!(\d+)(.)|([?-~])|(\$)|(-)|\x1b\\')


def decode(data: str) -> tuple[Rows, int, int]:
    """Return (rows of RGB or None, width, height); the size is the one the raster attributes declare."""
    palette: dict[int, tuple[int, int, int]] = {}
    pixels: dict[tuple[int, int], tuple[int, int, int]] = {}
    declared = (0, 0)
    colour, x, band = 0, 0, 0
    pos = 0
    while pos < len(data):
        m = _TOKEN.match(data, pos)
        if not m:
            raise ValueError(f"bad sixel byte at {pos}: {data[pos]!r}")
        pos = m.end()
        if m[2] is not None:
            attrs = [int(v) for v in m[2].split(";") if v]
            declared = (attrs[2], attrs[3])
        elif m[3] is not None:
            colour = int(m[3])
            if m[4] is not None:
                assert m[4] == "2", "only RGB colours"
                palette[colour] = tuple(round(int(v) * 255 / 100) for v in m.group(5, 6, 7))
        elif m[8] is not None or m[10] is not None:
            count, char = (int(m[8]), m[9]) if m[8] is not None else (1, m[10])
            value = ord(char) - 63
            for _ in range(count):
                for bit in range(6):
                    if value >> bit & 1:
                        pixels[(x, band * 6 + bit)] = palette[colour]
                x += 1
        elif m[11] is not None:
            x = 0
        elif m[12] is not None:
            x, band = 0, band + 1
    width, height = declared
    return [[pixels.get((cx, cy)) for cx in range(width)] for cy in range(height)], width, height


# --- golden images (SX) -------------------------------------------------------------------------------------------

HOLE_RGB = (255, 0, 255)  # a pixel the stream never drew is stored as magenta so a hole shows in the golden


def write_ppm(path: Path, rows: Rows) -> None:
    """Binary PPM (P6). Undrawn pixels (None) are written as HOLE_RGB. Mark the file `-text` in .gitattributes."""
    height, width = len(rows), len(rows[0]) if rows else 0
    body = bytearray()
    for row in rows:
        for px in row:
            body += bytes(px if px is not None else HOLE_RGB)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + bytes(body))


def read_ppm(path: Path) -> Rows:
    raw = path.read_bytes()
    m = re.match(rb"P6\s+(\d+)\s+(\d+)\s+255\s", raw)
    if not m:
        raise ValueError(f"{path} is not a binary PPM")
    width, height = int(m[1]), int(m[2])
    body = raw[m.end() :]
    if len(body) != width * height * 3:
        raise ValueError(f"{path}: {len(body)} bytes of pixels, expected {width * height * 3}")
    rows: Rows = []
    for y in range(height):
        base = y * width * 3
        rows.append([(body[base + 3 * x], body[base + 3 * x + 1], body[base + 3 * x + 2]) for x in range(width)])
    return rows


@dataclass(frozen=True)
class Diff:
    width: int
    height: int
    bad: int  # pixels whose channels differ by more than the tolerance
    bad_fraction: float
    worst_row: int
    worst_row_fraction: float  # the largest share of one row's pixels that is bad

    def __str__(self) -> str:
        return (f"{self.bad} bad pixels of {self.width * self.height} ({self.bad_fraction:.4%}); "
                f"worst row {self.worst_row} has {self.worst_row_fraction:.1%} bad")


def compare(got: Rows, want: Rows, channel_tol: int = 8) -> Diff:
    """Pixel difference between two decoded images. Undrawn pixels compare as HOLE_RGB, like in a golden file."""
    if (len(got), len(got[0]) if got else 0) != (len(want), len(want[0]) if want else 0):
        raise AssertionError(f"size {len(got[0]) if got else 0}x{len(got)} differs from golden {len(want[0]) if want else 0}x{len(want)}")
    bad = worst = 0
    worst_row = 0
    for y, (a, b) in enumerate(zip(got, want, strict=True)):
        row_bad = 0
        for pa, pb in zip(a, b, strict=True):
            pa, pb = pa or HOLE_RGB, pb or HOLE_RGB
            if abs(pa[0] - pb[0]) > channel_tol or abs(pa[1] - pb[1]) > channel_tol or abs(pa[2] - pb[2]) > channel_tol:
                row_bad += 1
        bad += row_bad
        if row_bad > worst:
            worst, worst_row = row_bad, y
    width, height = len(got[0]), len(got)
    return Diff(width, height, bad, bad / (width * height), worst_row, worst / width)


def assert_matches(got: Rows, want: Rows, channel_tol: int = 8, max_bad_fraction: float = 0.002,
                   max_row_fraction: float = 0.2) -> Diff:
    """Scattered small differences are tolerated (anti-aliasing, a palette rounding); a whole wrong pixel row is not.

    Two limits, both must hold: at most `max_bad_fraction` of all pixels differ by more than `channel_tol`, and no
    single row has more than `max_row_fraction` of its pixels wrong. The second one is what a one-row error trips: a
    row is a tiny share of the image, so the first limit alone would let it through.
    """
    diff = compare(got, want, channel_tol)
    assert diff.bad_fraction <= max_bad_fraction, f"image differs from golden: {diff}"
    assert diff.worst_row_fraction <= max_row_fraction, f"a pixel row differs from golden: {diff}"
    return diff


def mutate_pixel_row(data: str, y: int) -> str:
    """The same sixel stream with pixel row `y` wrong: every data character of band y // 6 has bit y % 6 flipped."""
    band_of, bit = y // 6, y % 6
    out: list[str] = []
    band, pos = 0, 0
    changed = 0
    while pos < len(data):
        m = _TOKEN.match(data, pos)
        if not m:
            raise ValueError(f"bad sixel byte at {pos}: {data[pos]!r}")
        text = m.group(0)
        if band == band_of and (m[8] is not None or m[10] is not None):
            char = m[9] if m[8] is not None else m[10]
            flipped = chr(((ord(char) - 63) ^ (1 << bit)) + 63)
            text = f"!{m[8]}{flipped}" if m[8] is not None else flipped
            changed += 1
        elif m[12] is not None:
            band += 1
        out.append(text)
        pos = m.end()
    if not changed:
        raise ValueError(f"pixel row {y} is not in the stream")
    return "".join(out)
