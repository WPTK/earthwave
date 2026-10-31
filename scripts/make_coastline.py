"""Regenerate earthwave/data/coastline_10m.json from Natural Earth 1:10m coastline (public domain).

Dev-only: needs pyshp (in the dev extra). Usage: python scripts/make_coastline.py [--scale 10m] [--tolerance 0.01]
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

# Source: Natural Earth 1:10m physical vectors, ne_10m_coastline (public domain).
# Simplification is planar Douglas-Peucker in degrees (default 0.01, about 1 km), so it is slightly coarser in
# longitude at high latitudes. For comparison linecast uses the same 1:10m set at 0.012 degrees.
URL = "https://naciscdn.org/naturalearth/{scale}/physical/ne_{scale}_coastline.zip"
OUT = Path(__file__).resolve().parent.parent / "earthwave" / "data" / "coastline_10m.json"

Point = tuple[float, float]


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


def build(scale: str, tolerance: float) -> list[list[list[float]]]:
    with urllib.request.urlopen(URL.format(scale=scale), timeout=120) as resp:
        data = resp.read()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        stem = next(n[:-4] for n in zf.namelist() if n.endswith(".shp"))
        reader = shapefile.Reader(
            shp=io.BytesIO(zf.read(stem + ".shp")),
            shx=io.BytesIO(zf.read(stem + ".shx")),
            dbf=io.BytesIO(zf.read(stem + ".dbf")),
        )
    lines: list[list[list[float]]] = []
    for shape in reader.shapes():
        bounds = [*shape.parts, len(shape.points)]
        for start, end in itertools.pairwise(bounds):
            pts = [(float(x), float(y)) for x, y in shape.points[start:end]]
            simple = douglas_peucker(pts, tolerance)
            if len(simple) >= 2:
                lines.append([[round(x, 3), round(y, 3)] for x, y in simple])
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tolerance", type=float, default=0.01, help="Douglas-Peucker tolerance in degrees")
    ap.add_argument("--scale", default="10m", choices=["10m", "50m", "110m"], help="Natural Earth scale")
    args = ap.parse_args()
    lines = build(args.scale, args.tolerance)
    OUT.write_text(json.dumps(lines, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
    n = sum(len(line) for line in lines)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {len(lines)} polylines, {n} points)")


if __name__ == "__main__":
    main()
