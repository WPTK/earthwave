"""Regenerate earthwave/data/coastline_10m.json from Natural Earth 1:10m coastline (public domain).

The file holds levels of detail of the same coastline, finest first. A braille dot is 0.1 to 2 degrees across, so
drawing the 0.01 degree line at a world zoom turns every island into a smear; the map picks the coarsest level whose
tolerance is under half a dot (`earthwave/ui/widgets/braille.py`). Each level is Douglas-Peucker simplified from the
full source line and drops polylines too small to light a dot at the finest zoom that uses it.

Dev-only: needs pyshp (in the dev extra). Usage: python scripts/make_coastline.py [--scale 10m]
"""

from __future__ import annotations

import argparse
import io
import itertools
import json
import math
import urllib.request
import zipfile
from pathlib import Path

import shapefile

from earthwave import __version__

UA = f"earthwave/{__version__} (+https://github.com/WPTK/earthwave; coastline build)"
# Source: Natural Earth 1:10m physical vectors, ne_10m_coastline (public domain).
# Simplification is planar Douglas-Peucker in degrees, so it is slightly coarser in longitude at high latitudes.
URL = "https://naciscdn.org/naturalearth/{scale}/physical/ne_{scale}_coastline.zip"
OUT = Path(__file__).resolve().parent.parent / "earthwave" / "data" / "coastline_10m.json"

Point = tuple[float, float]

# (Douglas-Peucker tolerance, smallest polyline kept) in degrees. A level is used when its tolerance is at most half
# a dot, so a dot is at least twice the tolerance and a polyline under `min_extent` (one dot at that zoom) is dropped.
# Anything that spans a whole 2x4 dot cell at the zoom where it is used is far above the minimum and always kept.
LEVELS = ((0.01, 0.0), (0.1, 0.2), (0.4, 0.8))


def _dist(p: Point, a: Point, b: Point) -> float:
    """Distance from p to segment a-b in degrees (planar)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == 0 and dy == 0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def douglas_peucker(points: list[Point], tolerance: float) -> list[Point]:
    """Iterative Douglas-Peucker; keeps both endpoints."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        lo, hi = stack.pop()
        far, far_d = -1, tolerance
        for i in range(lo + 1, hi):
            d = _dist(points[i], points[lo], points[hi])
            if d > far_d:
                far, far_d = i, d
        if far >= 0:
            keep[far] = True
            stack.append((lo, far))
            stack.append((far, hi))
    return [p for p, k in zip(points, keep, strict=True) if k]


def _unwrap(points: list[Point]) -> list[Point]:
    """Continuous longitudes (no jump over 180), so simplification never sees a dateline crossing as a long chord."""
    out: list[Point] = []
    offset, prev = 0.0, None
    for lon, lat in points:
        if prev is not None:
            if lon - prev > 180:
                offset -= 360.0
            elif prev - lon > 180:
                offset += 360.0
        prev = lon
        out.append((lon + offset, lat))
    return out


def _wrap(lon: float) -> float:
    return (lon + 180.0) % 360.0 - 180.0 if abs(lon) > 180.0 else lon


def _extent(points: list[Point]) -> float:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return max(max(xs) - min(xs), max(ys) - min(ys))


def build(scale: str) -> list[dict]:
    request = urllib.request.Request(URL.format(scale=scale), headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=120) as resp:
        data = resp.read()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        stem = next(n[:-4] for n in zf.namelist() if n.endswith(".shp"))
        reader = shapefile.Reader(
            shp=io.BytesIO(zf.read(stem + ".shp")),
            shx=io.BytesIO(zf.read(stem + ".shx")),
            dbf=io.BytesIO(zf.read(stem + ".dbf")),
        )
    sources: list[list[Point]] = []
    for shape in reader.shapes():
        bounds = [*shape.parts, len(shape.points)]
        for start, end in itertools.pairwise(bounds):
            sources.append(_unwrap([(float(x), float(y)) for x, y in shape.points[start:end]]))
    levels = []
    for tolerance, min_extent in LEVELS:
        lines = []
        for pts in sources:
            if min_extent and _extent(pts) < min_extent:
                continue
            simple = douglas_peucker(pts, tolerance)
            if len(simple) >= 2:
                lines.append([[round(_wrap(x), 3), round(y, 3)] for x, y in simple])
        levels.append({"tolerance": tolerance, "min_extent": min_extent, "lines": lines})
    return levels


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scale", default="10m", choices=["10m", "50m", "110m"], help="Natural Earth scale")
    args = ap.parse_args()
    levels = build(args.scale)
    OUT.write_text(json.dumps(levels, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    for level in levels:
        n = sum(len(line) for line in level["lines"])
        print(f"  tolerance {level['tolerance']}: {len(level['lines'])} polylines, {n} points")


if __name__ == "__main__":
    main()
