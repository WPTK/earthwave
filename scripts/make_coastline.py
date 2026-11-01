"""Regenerate earthwave/data/coastline_10m.json and land_10m.json from Natural Earth 1:10m (public domain).

The file holds levels of detail of the same coastline, finest first. A braille dot is 0.1 to 2 degrees across, so
drawing the 0.01 degree line at a world zoom turns every island into a smear; the map picks the coarsest level whose
tolerance is under half a dot (`earthwave/ui/widgets/braille.py`). Each level is Douglas-Peucker simplified from the
full source line and drops polylines too small to light a dot at the finest zoom that uses it.

The land file is the ne_10m_land polygons (rings, holes included) simplified the same way but coarser, one level per
coastline level: the map fills it under the coastline, and a fill only has to be right to a cell (2x4 dots).
Sources: ne_10m_coastline and ne_10m_land, naturalearthdata.com, public domain (no attribution required).

Dev-only: needs pyshp (in the dev extra). Usage: python scripts/make_coastline.py [--scale 10m] [--from-dir DIR]
(`--from-dir` reads ne_<scale>_coastline.zip and ne_<scale>_land.zip from DIR instead of downloading them.)
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
URL = "https://naciscdn.org/naturalearth/{scale}/physical/ne_{scale}_{name}.zip"
DATA = Path(__file__).resolve().parent.parent / "earthwave" / "data"
OUT = DATA / "coastline_10m.json"
LAND_OUT = DATA / "land_10m.json"

Point = tuple[float, float]

# (Douglas-Peucker tolerance, smallest polyline kept) in degrees. A level is used when its tolerance is at most half
# a dot, so a dot is at least twice the tolerance and a polyline under `min_extent` (one dot at that zoom) is dropped.
# Anything that spans a whole 2x4 dot cell at the zoom where it is used is far above the minimum and always kept.
LEVELS = ((0.01, 0.0), (0.1, 0.2), (0.4, 0.8))
LAND_LEVELS = ((0.03, 0.0), (0.15, 0.3), (0.4, 0.8))


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


def read_sources(scale: str, name: str, from_dir: Path | None) -> list[list[Point]]:
    """Every polyline (or polygon ring) of one Natural Earth zip, with continuous longitudes."""
    if from_dir is not None:
        data = (from_dir / f"ne_{scale}_{name}.zip").read_bytes()
    else:
        request = urllib.request.Request(URL.format(scale=scale, name=name), headers={"User-Agent": UA})
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
    return sources


def simplify_levels(sources: list[list[Point]], spec: tuple[tuple[float, float], ...], ring: bool) -> list[dict]:
    levels = []
    for tolerance, min_extent in spec:
        lines = []
        for pts in sources:
            if min_extent and _extent(pts) < min_extent:
                continue
            simple = douglas_peucker(pts, tolerance)
            if len(simple) >= (4 if ring else 2):  # a ring needs a triangle plus its closing point
                lines.append([[round(_wrap(x), 3), round(y, 3)] for x, y in simple])
        levels.append({"tolerance": tolerance, "min_extent": min_extent, "lines": lines})
    return levels


def build(scale: str, from_dir: Path | None = None) -> tuple[list[dict], list[dict]]:
    coast = simplify_levels(read_sources(scale, "coastline", from_dir), LEVELS, ring=False)
    land = simplify_levels(read_sources(scale, "land", from_dir), LAND_LEVELS, ring=True)
    return coast, land


def write(path: Path, levels: list[dict]) -> None:
    path.write_text(json.dumps(levels, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {path} ({path.stat().st_size} bytes)")
    for level in levels:
        n = sum(len(line) for line in level["lines"])
        print(f"  tolerance {level['tolerance']}: {len(level['lines'])} polylines, {n} points")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scale", default="10m", choices=["10m", "50m", "110m"], help="Natural Earth scale")
    ap.add_argument("--from-dir", type=Path, help="directory holding the Natural Earth zips (skips the download)")
    args = ap.parse_args()
    coast, land = build(args.scale, args.from_dir)
    write(OUT, coast)
    write(LAND_OUT, land)


if __name__ == "__main__":
    main()
