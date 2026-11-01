"""Sixel encoder for palette images, pure Python with no dependencies.

A sixel image is a stream of 6-pixel-tall bands. Per band and palette colour the encoder builds one character per
column (bit i set when row i of the band has that colour) and run-length compresses it. The bit work is done on whole
rows at once with `bytes.translate` and big-integer shifts, so a full-screen image takes a fraction of a second.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from functools import cache

RGB = tuple[int, int, int]
HOLE = 255  # pixel value that is not painted

_ADD_63 = bytes((i + 63) & 0xFF for i in range(256))
_RUN = re.compile(rb"(.)\1{3,}", re.DOTALL)


@cache
def _mask_table(index: int) -> bytes:
    """Translation table sending palette index `index` to 1 and every other index to 0."""
    return bytes(1 if i == index else 0 for i in range(256))


def _run_length(data: bytes) -> bytes:
    return _RUN.sub(lambda m: b"!%d%s" % (len(m[0]), m[1]), data)


def _band_data(rows: Sequence[bytes], width: int, colour: int) -> bytes:
    """Sixel characters of one colour for one band (at most 6 rows), run-length compressed, trailing blanks cut."""
    table = _mask_table(colour)
    bits = 0
    for i, row in enumerate(rows):
        bits |= int.from_bytes(row.translate(table), "little") << i
    return _run_length(bits.to_bytes(width, "little").translate(_ADD_63).rstrip(b"?"))


def sixel_rgb(colour: RGB) -> RGB:
    """The colour a terminal ends up with: sixel palettes hold percentages, so each channel is quantised to 1/100."""
    return tuple(round(round(c * 100 / 255) * 255 / 100) for c in colour)  # type: ignore[return-value]


def encode(pixels: bytes, width: int, height: int, palette: Sequence[RGB]) -> str:
    """Sixel string (DCS ... ST) for `pixels`, row-major palette indices, `width` x `height`.

    Every pixel is written (colour 0 included), so the image is opaque and the terminal background never shows
    through, except pixels equal to `HOLE`, which are left unpainted so what is under them (a toast) stays.
    `height` need not be a multiple of 6; the last band is padded with blank rows.
    """
    if len(pixels) != width * height:
        raise ValueError("pixel buffer does not match width x height")
    if pixels and max((p for p in set(pixels) if p != HOLE), default=0) >= len(palette):
        raise ValueError("pixel index outside the palette")
    out = [b'\x1bP0;1;0q"1;1;%d;%d' % (width, height)]
    for index, (r, g, b) in enumerate(palette):
        out.append(b"#%d;2;%d;%d;%d" % (index, *(round(c * 100 / 255) for c in (r, g, b))))
    blank = bytes(width)
    for top in range(0, height, 6):
        end = min(top + 6, height)
        rows = [pixels[y * width : (y + 1) * width] for y in range(top, end)]
        rows += [blank] * (6 - len(rows))
        for colour in sorted(set(pixels[top * width : end * width]) - {HOLE}):
            out.append(b"#%d%s$" % (colour, _band_data(rows, width, colour)))
        out.append(b"-")
    out.append(b"\x1b\\")
    return b"".join(out).decode("ascii")
