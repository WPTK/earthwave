"""A small independent sixel decoder for tests: parses the stream into rows of RGB triples (None = never drawn)."""

import re

_TOKEN = re.compile(r'\x1bP([\d;]*)q|"([\d;]*)|#(\d+)(?:;(\d+);(\d+);(\d+);(\d+))?|!(\d+)(.)|([?-~])|(\$)|(-)|\x1b\\')


def decode(data: str) -> tuple[list[list[tuple[int, int, int] | None]], int, int]:
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
