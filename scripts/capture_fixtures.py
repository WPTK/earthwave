"""Capture live fixtures under tests/fixtures/<source>/ (one polite request per URL)."""

import json
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

import earthwave

ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
UA = f"earthwave/{earthwave.__version__} (+https://github.com/WPTK/earthwave; fixture capture)"
ATOM = "{http://www.w3.org/2005/Atom}"
USGS = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary"
COOPS = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"


def save(client: httpx.Client, url: str, rel: str, params: dict | None = None) -> bytes:
    resp = client.get(url, params=params)
    resp.raise_for_status()
    path = ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(resp.content)
    print(f"{rel}  {len(resp.content)} bytes")
    time.sleep(1)
    return resp.content


def main() -> int:
    with httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True) as c:
        for feed in ("PAAQ", "PHEB"):
            raw = save(c, f"https://www.tsunami.gov/events/xml/{feed}Atom.xml", f"tsunamigov/{feed}Atom.xml")
            entries = ET.fromstring(raw).findall(f"{ATOM}entry")
            if not entries:
                print(f"warning: {feed} Atom feed has no entries; no CAP or bulletin captured", file=sys.stderr)
            for n, entry in enumerate(entries):
                for link in entry.findall(f"{ATOM}link"):
                    rel, href = link.get("rel"), link.get("href")
                    if rel == "related":
                        save(c, href, f"tsunamigov/{feed}_{n}.cap.xml")
                    elif rel == "alternate":
                        save(c, href, f"tsunamigov/{feed}_{n}.txt")

        for name in ("all_hour", "4.5_week"):
            save(c, f"{USGS}/{name}.geojson", f"usgs/{name}.geojson")
        sig = json.loads(save(c, f"{USGS}/significant_month.geojson", "usgs/significant_month.geojson"))
        # Prefer an event whose detail carries both products; fall back to the first with a detail URL.
        for feat in sig["features"]:
            types = feat["properties"].get("types") or ""
            if "losspager" in types and "shakemap" in types:
                save(c, feat["properties"]["detail"], "usgs/detail.geojson")
                break
        else:
            print("no significant event with losspager+shakemap; earlier files were already written", file=sys.stderr)
            return 1

        save(c, "https://www.ndbc.noaa.gov/activestations.xml", "ndbc/activestations.xml")
        save(c, "https://www.ndbc.noaa.gov/data/realtime2/46402.dart", "ndbc/46402.dart")
        save(
            c,
            COOPS,
            "coops/water_level_9414290.json",
            {
                "product": "water_level",
                "station": "9414290",
                "datum": "MLLW",
                "units": "metric",
                "time_zone": "gmt",
                "format": "json",
                "range": 6,
            },
        )
        jma_list = json.loads(save(c, "https://www.jma.go.jp/bosai/tsunami/data/list.json", "jma/list.json"))
        save(c, f"https://www.jma.go.jp/bosai/tsunami/data/{jma_list[0]['json']}", "jma/detail.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
