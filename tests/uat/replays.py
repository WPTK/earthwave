"""Crafted recordings for the journeys: the same JSONL the app's own ``--record`` writes, edited so a feed is empty, a
product is odd, an answer is an error or the data stops arriving. ``--replay`` serves them through the app's real
network stack (transport swapped, everything above it unmodified), so no hook, flag or build of the app is involved.

``Rec`` starts from a shipped recording (so the shapes are real) and offers a few edits. Recordings are served per URL,
the n-th request getting the n-th record, then the last one repeating; the replay clock jumps to each record's ``ts``
and otherwise moves at ``--speed`` times real time.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from collections.abc import Callable
from pathlib import Path
from typing import Self

from uat.target import KAM, USGS

USGS_FEEDS = ("4.5_week.geojson", "all_day.geojson", "significant_month.geojson", "2.5_day.geojson")
TSUNAMI_ATOM = "tsunami.gov/events/xml/"
NWS = "api.weather.gov/alerts/active"
JMA = "jma.go.jp/bosai/tsunami/data/list.json"


def empty_usgs(ts: str) -> str:
    """An empty but valid USGS feed made at the record's own time (one made in the replay's future is not believed)."""
    ms = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    return json.dumps({"type": "FeatureCollection", "metadata": {"generated": ms, "status": 200, "count": 0}, "features": []})


EMPTY_ATOM = (
    '<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
    "<id>urn:uuid:empty</id><title>none</title><updated>2025-07-29T23:00:00Z</updated></feed>"
)
EMPTY_NWS = json.dumps({"type": "FeatureCollection", "features": [], "title": "current watches, warnings, and advisories"})


class Rec:
    def __init__(self, rows: list[dict]):
        self.rows = [dict(r) for r in rows]

    @classmethod
    def load(cls, path: Path) -> Self:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for r in rows:  # the shipped recordings predate the app asking NWS for status=actual; without it every replay 404s
            if NWS in r["url"] and "status=actual" not in r["url"]:
                r["url"] += "&status=actual"
        return cls(rows)

    @classmethod
    def usgs(cls) -> Self:
        return cls.load(USGS)

    @classmethod
    def kamchatka(cls) -> Self:
        return cls.load(KAM)

    def matching(self, part: str) -> list[dict]:
        return [r for r in self.rows if part in r["url"]]

    def edit(self, part: str, fn: Callable[[dict], None]) -> Self:
        hit = self.matching(part)
        assert hit, f"no record for {part!r}"
        for r in hit:
            fn(r)
        return self

    def body(self, part: str, fn: Callable[[str], str]) -> Self:
        return self.edit(part, lambda r: r.__setitem__("body", fn(r.get("body", ""))))

    def status(self, part: str, status: int, body: str | None = None, *, after: str | None = None) -> Self:
        """Make every record for ``part`` answer ``status`` (with ``body``). ``after`` keeps the first record as is
        and turns the later ones into the failure: the feed worked once, then stopped."""
        hit = self.matching(part)
        assert hit, f"no record for {part!r}"
        targets = hit[1:] if after else hit
        if after and len(hit) == 1:  # a single record repeats forever: add the failing one after it
            extra = dict(hit[0])
            self.rows.append(extra)
            targets = [extra]
        for r in targets:
            r["status"] = status
            if body is not None:
                r["body"] = body
            r.pop("body_b64", None)
        return self

    def drop(self, part: str) -> Self:
        self.rows = [r for r in self.rows if part not in r["url"]]
        return self

    def only_from(self, ts: str) -> Self:
        """Keep the first record of every URL and every later record at or after ``ts`` (shortens a long replay)."""
        seen: set[str] = set()
        keep = []
        for r in self.rows:
            if r["url"] not in seen or r["ts"] >= ts:
                keep.append(r)
            seen.add(r["url"])
        self.rows = keep
        return self

    def save(self, path: Path) -> Path:
        path.write_text("\n".join(json.dumps(r) for r in self.rows) + "\n", encoding="utf-8")
        return path


def all_quiet() -> Rec:
    """Every feed valid and empty: a quiet day."""
    rec = Rec.kamchatka()
    for feed in USGS_FEEDS:
        rec.edit(feed, lambda r: r.__setitem__("body", empty_usgs(r["ts"])))
    rec.body(TSUNAMI_ATOM, lambda _b: EMPTY_ATOM)
    rec.body(NWS, lambda _b: EMPTY_NWS)
    rec.body(JMA, lambda _b: "[]")
    return rec.drop("CAP.xml").drop("WEAK51.txt")


def warning_with_expiry(expires: str) -> Rec:
    """The Kamchatka recording reduced to one product: the Warning, whose ``<expires>`` is replaced by ``expires``
    (an ISO time, which may be earlier than the product's ``<sent>``, as tsunami.gov published once, #245)."""
    rec = Rec.kamchatka()
    warning = "kam001/2/WEAK51"
    atom = next(r for r in rec.matching("PAAQAtom.xml") if warning in r["body"] and "kam001/3/" not in r["body"])
    entry = re.search(r"<entry>(?:(?!</entry>).)*?" + re.escape(warning) + r"(?:(?!</entry>).)*?</entry>", atom["body"], re.S)
    assert entry, "the Warning entry is not in the Atom record"
    atom["body"] = atom["body"].split("<entry>")[0] + entry.group(0) + "</feed>"
    atom["ts"] = "2025-07-29T23:52:00Z"
    keep = [r for r in rec.rows if "earthquake.usgs.gov" in r["url"] or "jma.go.jp" in r["url"] or "PHEBAtom" in r["url"]]
    keep += [atom] + [r for r in rec.rows if warning in r["url"]]
    nws = rec.matching(NWS)[0]
    nws["body"] = EMPTY_NWS
    keep.append(nws)
    for r in keep:
        if warning in r["url"]:
            r["ts"] = "2025-07-29T23:52:00Z"
    rec.rows = keep
    rec.body("kam001/2/WEAK51/PAAQCAP.xml", lambda b: re.sub(r"<expires>[^<]*</expires>", f"<expires>{expires}</expires>", b))
    return rec
