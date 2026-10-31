# ruff: noqa: E501
"""Write the synthesized Kamchatka Watch -> Warning -> Cancellation fixtures and their replay recording.

The products follow the structure of the captured tsunami.gov CAP files (tests/fixtures/tsunamigov/PAAQ_0.cap.xml)
and the NWS alerts API, with the timeline of the 2025-07-29 Kamchatka M8.8 event. They are synthesized, not archived
copies: the per-event URLs of the real products could not be reached. Re-run after changing anything below:

    python scripts/make_tsunami_fixtures.py
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape

import httpx

ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
OUT = ROOT / "tsunamigov" / "kamchatka"
BASE = "https://www.tsunami.gov"
NWS_URL = str(
    httpx.Request(
        "GET",
        "https://api.weather.gov/alerts/active",
        params={"event": "Tsunami Warning,Tsunami Advisory,Tsunami Watch"},
    ).url
)
QUAKE = {"id": "us6000qw60", "lat": 52.512, "lon": 160.324, "depth": 20.7, "time": "2025-07-29T23:24:52Z"}
ORIGIN = "2025-07-29T23:24:52-00:00"


def iso(text: str) -> str:
    return text.replace("Z", "-00:00")


PRODUCTS = [
    {
        "seq": 1,
        "ident": "PAAQ-1-kam001",
        "sent": "2025-07-29T23:34:12Z",
        "expires": "2025-07-30T01:34:12Z",
        "msg": "Alert",
        "event": "Tsunami Watch",
        "severity": "Moderate",
        "urgency": "Expected",
        "certainty": "Possible",
        "mag": "8.0",
        "areas": ["Alaska Peninsula", "Aleutian Islands"],
        "header": "WEAK51 PAAQ 292334",
        "references": [],
        "headline": "TSUNAMI WATCH NUMBER 1",
        "body": "A tsunami watch is in effect for the Alaska Peninsula and the Aleutian Islands. A tsunami is possible.",
    },
    {
        "seq": 2,
        "ident": "PAAQ-2-kam002",
        "sent": "2025-07-29T23:51:07Z",
        "expires": "2025-07-30T03:51:07Z",
        "msg": "Update",
        "event": "Tsunami Warning",
        "severity": "Severe",
        "urgency": "Immediate",
        "certainty": "Likely",
        "mag": "8.7",
        "areas": ["Aleutian Islands", "Alaska Peninsula", "Kodiak Island"],
        "header": "WEAK51 PAAQ 292351",
        "references": ["PAAQ-1-kam001"],
        "headline": "TSUNAMI WARNING NUMBER 2",
        "body": "A tsunami warning is in effect. Tsunami waves are expected. Move to high ground or inland now.",
    },
    {
        "seq": 3,
        "ident": "PAAQ-3-kam003",
        "sent": "2025-07-30T01:40:12Z",
        "expires": "2025-07-30T02:40:12Z",
        "msg": "Cancel",
        "event": "Tsunami Warning",
        "severity": "Minor",
        "urgency": "Past",
        "certainty": "Unlikely",
        "mag": "8.8",
        "areas": ["Aleutian Islands", "Alaska Peninsula", "Kodiak Island"],
        "header": "WEAK51 PAAQ 300140",
        "references": ["PAAQ-2-kam002"],
        "headline": "TSUNAMI WARNING CANCELLED",
        "body": "The tsunami warning is cancelled. The tsunami threat has passed.",
    },
]


def url(p: dict, name: str) -> str:
    day = p["sent"][:10].replace("-", "/")
    return f"{BASE}/events/PAAQ/{day}/kam001/{p['seq']}/WEAK51/{name}"


def cap_xml(p: dict) -> str:
    refs = " ".join(f"ntwc@noaa.gov,{r},{iso(PRODUCTS[int(r.split('-')[1]) - 1]['sent'])}" for r in p["references"])
    refs_xml = f"\n  <references>{refs}</references>" if refs else ""
    areas = "\n".join(
        f"    <area>\n      <areaDesc>{escape(a)}</areaDesc>\n      <circle>52.5,160.3 0.0</circle>\n    </area>"
        for a in p["areas"]
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?><alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>{p["ident"]}</identifier>
  <sender>ntwc@noaa.gov</sender>
  <sent>{iso(p["sent"])}</sent>
  <status>Actual</status>
  <msgType>{p["msg"]}</msgType>
  <source>NTWC</source>
  <scope>Public</scope>
  <code>IPAWSv1.0</code>{refs_xml}
  <info>
    <category>Geo</category>
    <event>{p["event"]}</event>
    <responseType>Prepare</responseType>
    <urgency>{p["urgency"]}</urgency>
    <severity>{p["severity"]}</severity>
    <certainty>{p["certainty"]}</certainty>
    <onset>{iso(p["sent"])}</onset>
    <expires>{iso(p["expires"])}</expires>
    <senderName>NWS NATIONAL TSUNAMI WARNING CENTER PALMER AK</senderName>
    <headline>{p["headline"]}</headline>
    <description>{escape(p["body"])}</description>
    <instruction>  N/A </instruction>
    <web>{url(p, "WEAK51.txt")}</web>
    <parameter>
      <valueName>EventLocationName</valueName>
      <value>OFF EAST COAST OF KAMCHATKA</value>
    </parameter>
    <parameter>
      <valueName>EventPreliminaryMagnitude</valueName>
      <value>{p["mag"]}</value>
    </parameter>
    <parameter>
      <valueName>EventPreliminaryMagnitudeType</valueName>
      <value>Mww</value>
    </parameter>
    <parameter>
      <valueName>EventOriginTime</valueName>
      <value>{ORIGIN}</value>
    </parameter>
    <parameter>
      <valueName>EventLatLon</valueName>
      <value>52.500,160.300 0.000</value>
    </parameter>
    <parameter>
      <valueName>WEAK51</valueName>
      <value>Tsunami Warnings, Advisories and Watches for AK, BC, and US West Coast</value>
    </parameter>
{areas}
  </info>
</alert>
"""


def bulletin(p: dict) -> str:
    return (
        f"\nZCZC\n{p['header']}\nTSUAK1\n\n{p['headline'].title()}\n"
        f"NWS National Tsunami Warning Center Palmer AK\n\n"
        f"...{p['event'].upper()}{' CANCELLED' if p['msg'] == 'Cancel' else ''}...\n\n"
        f"EVALUATION\n----------\n\n  * AN EARTHQUAKE WITH A PRELIMINARY MAGNITUDE OF {p['mag']} OCCURRED OFF THE\n"
        f"    EAST COAST OF KAMCHATKA ON TUESDAY JULY 29 2025.\n\n  * {p['body'].upper()}\n\n"
        f"AFFECTED AREAS\n--------------\n\n" + "".join(f"  * {a.upper()}\n" for a in p["areas"]) + "\n$$\n"
    )


def atom(entries: list[dict]) -> str:
    body = ""
    for p in entries:
        body += f"""
   <entry>
    <title>{escape(p["event"])} Number {p["seq"]}</title><updated>{p["sent"]}</updated>
<geo:lat>52.500</geo:lat>
<geo:long>160.300</geo:long>
<summary type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml">
     <strong>Category:</strong> {escape(p["event"])}<br/><strong>Preliminary Magnitude: </strong>{p["mag"]}(Mww)<br/>
</div></summary>
    <id>urn:uuid:5b0e7c10-0000-4000-8000-00000000000{p["seq"]}</id><link rel="related" title="CapXML document" href="{url(p, "PAAQCAP.xml")}" type="application/cap+xml" />
<link rel="alternate" title="Bulletin" href="{url(p, "WEAK51.txt")}" type="application/xml" />
</entry>"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom" xmlns:geo="http://www.w3.org/2003/01/geo/wgs84_pos#" xmlns:georss="http://www.georss.org/georss">
    <id>urn:uuid:5b0e7c10-feed-4000-8000-000000000000</id><title>{escape(entries[0]["headline"].title()) if entries else "No events"}</title>
    <updated>{entries[0]["sent"] if entries else "2025-07-29T23:00:00Z"}</updated>
<link type="application/atom+xml" rel="self" title="self" href="{BASE}/events/xml/PAAQAtom.xml"/>
{body}
</feed>
"""


def nws_feature(p: dict) -> dict:
    ring = [[-176.0, 51.0], [-176.0, 55.5], [-158.0, 57.0], [-155.0, 55.0], [-165.0, 52.0], [-176.0, 51.0]]
    return {
        "id": f"https://api.weather.gov/alerts/urn:oid:2.49.0.1.840.0.kam{p['seq']:03d}",
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {
            "id": f"urn:oid:2.49.0.1.840.0.kam{p['seq']:03d}",
            "areaDesc": "Aleutian Islands; Alaska Peninsula; Kodiak Island",
            "sent": iso(p["sent"]).replace("-00:00", "+00:00"),
            "effective": iso(p["sent"]).replace("-00:00", "+00:00"),
            "expires": iso(p["expires"]).replace("-00:00", "+00:00"),
            "ends": None,
            "status": "Actual",
            "messageType": p["msg"],
            "severity": p["severity"],
            "certainty": p["certainty"],
            "urgency": p["urgency"],
            "event": p["event"],
            "headline": f"{p['event']} issued July 29 at 3:51PM AKDT by NWS National Tsunami Warning Center",
            "description": p["body"],
            "instruction": "Move to high ground or inland now.",
            "references": [],
            "parameters": {"WMOidentifier": [f"{p['header']}"], "AWIPSidentifier": ["TSUAK1"]},
        },
    }


def feed(features: list[dict]) -> str:
    return json.dumps({"type": "FeatureCollection", "features": features}, indent=1) + "\n"


def usgs_feed(mag: float | None, generated: str) -> str:
    features = []
    if mag is not None:
        t = datetime.fromisoformat(QUAKE["time"].replace("Z", "+00:00"))
        features.append(
            {
                "type": "Feature",
                "id": QUAKE["id"],
                "geometry": {"type": "Point", "coordinates": [QUAKE["lon"], QUAKE["lat"], QUAKE["depth"]]},
                "properties": {
                    "mag": mag,
                    "place": "125 km ESE of Petropavlovsk-Kamchatsky, Russia",
                    "time": int(t.timestamp() * 1000),
                    "updated": int(datetime.fromisoformat(generated.replace("Z", "+00:00")).timestamp() * 1000),
                    "tsunami": 1,
                    "sig": 2000,
                    "alert": "red" if mag >= 8.5 else None,
                    "status": "reviewed" if mag >= 8.5 else "automatic",
                    "magType": "mww",
                    "type": "earthquake",
                    "url": f"https://earthquake.usgs.gov/earthquakes/eventpage/{QUAKE['id']}",
                    "detail": f"https://earthquake.usgs.gov/earthquakes/feed/v1.0/detail/{QUAKE['id']}.geojson",
                    "ids": f",{QUAKE['id']},",
                },
            }
        )
    return json.dumps({"type": "FeatureCollection", "metadata": {"generated": 0}, "features": features})


def record(ts: str, url_: str, body: str, status: int = 200) -> dict:
    return {"ts": ts, "method": "GET", "url": url_, "status": status, "headers": {}, "body": body}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for p in PRODUCTS:
        (OUT / f"{p['seq']}_{p['event'].split()[-1].lower()}_{p['msg'].lower()}.cap.xml").write_text(
            cap_xml(p), encoding="utf-8", newline="\n"
        )
        (OUT / f"{p['seq']}_{p['event'].split()[-1].lower()}_{p['msg'].lower()}.txt").write_text(
            bulletin(p), encoding="utf-8", newline="\n"
        )
    nws = ROOT / "nws"
    nws.mkdir(parents=True, exist_ok=True)
    (nws / "tsunami_warning.geojson").write_text(feed([nws_feature(PRODUCTS[1])]), encoding="utf-8", newline="\n")

    usgs = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/"
    pa, ph = f"{BASE}/events/xml/PAAQAtom.xml", f"{BASE}/events/xml/PHEBAtom.xml"
    watch, warn, cancel = PRODUCTS
    records = [
        record("2025-07-29T23:28:00Z", f"{usgs}4.5_week.geojson", usgs_feed(None, "2025-07-29T23:28:00Z")),
        record("2025-07-29T23:28:01Z", f"{usgs}all_day.geojson", usgs_feed(None, "2025-07-29T23:28:01Z")),
        record("2025-07-29T23:28:02Z", f"{usgs}significant_month.geojson", usgs_feed(None, "2025-07-29T23:28:02Z")),
        record("2025-07-29T23:28:10Z", f"{usgs}2.5_day.geojson", usgs_feed(None, "2025-07-29T23:28:10Z")),
        record("2025-07-29T23:33:10Z", f"{usgs}2.5_day.geojson", usgs_feed(8.0, "2025-07-29T23:33:10Z")),
        record("2025-07-29T23:50:10Z", f"{usgs}2.5_day.geojson", usgs_feed(8.7, "2025-07-29T23:50:10Z")),
        record("2025-07-29T23:36:00Z", pa, atom([watch])),
        record("2025-07-29T23:51:30Z", pa, atom([warn, watch])),
        record("2025-07-30T01:41:00Z", pa, atom([cancel, warn, watch])),
        record("2025-07-29T23:36:00Z", ph, atom([])),
        record("2025-07-29T23:36:00Z", NWS_URL, feed([])),
        record("2025-07-29T23:52:00Z", NWS_URL, feed([nws_feature(warn)])),
        record("2025-07-30T01:41:00Z", NWS_URL, feed([])),
        record("2025-07-29T23:36:00Z", "https://www.jma.go.jp/bosai/tsunami/data/list.json", "[]"),
    ]
    for p in PRODUCTS:
        stem = f"{p['seq']}_{p['event'].split()[-1].lower()}_{p['msg'].lower()}"
        stamp = p["sent"].replace("T23:34:12", "T23:36:00").replace("T23:51:07", "T23:51:30")
        stamp = "2025-07-30T01:41:00Z" if p["seq"] == 3 else stamp
        records.append(record(stamp, url(p, "PAAQCAP.xml"), (OUT / f"{stem}.cap.xml").read_text(encoding="utf-8")))
        records.append(record(stamp, url(p, "WEAK51.txt"), (OUT / f"{stem}.txt").read_text(encoding="utf-8")))
    replay = ROOT / "replay" / "kamchatka_watch_to_warning.jsonl"
    # The recording starts at its first record, so keep the file ordered by time.
    records.sort(key=lambda r: datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).astimezone(UTC))
    replay.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {len(records)} records to {replay}")


if __name__ == "__main__":
    main()
