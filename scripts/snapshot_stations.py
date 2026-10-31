"""Regenerate earthwave/data/dart_stations.json from NDBC's activestations.xml.

Default input is the saved fixture; ``--live`` makes one polite request instead.
"""

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from earthwave.sources.ndbc import STATIONS_URL  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "ndbc" / "activestations.xml"
OUT = ROOT / "earthwave" / "data" / "dart_stations.json"


def build(xml: bytes) -> dict:
    """Every attribute of every ``type="dart"`` row, unpruned; rows without coordinates are skipped."""
    created = re.search(rb'created="([^"]+)"', xml)
    stations = []
    for el in ET.fromstring(xml).iter("station"):
        if el.get("type") != "dart":
            continue
        row = {k: v.strip() for k, v in el.attrib.items()}
        try:
            row["lat"], row["lon"] = float(row["lat"]), float(row["lon"])
        except (KeyError, ValueError):
            continue  # same rule as parse_active_stations
        row["kind"] = "dart"
        stations.append(row)
    stations.sort(key=lambda r: r["id"])
    return {
        "source": STATIONS_URL,
        "created": created.group(1).decode() if created else None,
        "stations": stations,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="fetch activestations.xml instead of using the fixture")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    if args.live:
        import httpx

        import earthwave

        ua = f"earthwave/{earthwave.__version__} (+https://github.com/WPTK/earthwave; station snapshot)"
        resp = httpx.get(STATIONS_URL, headers={"User-Agent": ua}, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        xml = resp.content
    else:
        xml = FIXTURE.read_bytes()
    data = build(xml)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    print(f"{args.out}  {len(data['stations'])} DART stations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
