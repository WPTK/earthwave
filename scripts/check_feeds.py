"""Fetch every live feed the app depends on, once, and run it through the app's own parsers.

Each feed ends in one of four states:
  ok           fetched and parsed
  empty        fetched and parsed, nothing in it (normal for alert feeds; not a format change)
  unavailable  network error, timeout, 5xx, 429 or 403 (the feed is down or refusing us; not a format change)
  format       the reply arrived but the app's parser cannot read it, or the URL is gone (404 / 410)

A format failure is raised at once. Unavailable, and empty for feeds that are never empty, only extend a streak kept
in the state file and are raised after STREAK_LIMIT consecutive runs. The workflow turns the result into an issue."""

import argparse
import json
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from earthwave.net import user_agent
from earthwave.sources import coops, jma, ndbc, nws, tsunamigov, usgs

STREAK_LIMIT = 3
PAUSE = 1.0  # seconds between requests
UA = user_agent("feed check")
GONE = {404, 410}
DOWN = {403, 408, 429}
# Feeds that always hold something. Alert feeds are empty almost every day, so their emptiness never raises.
NEVER_EMPTY = {"usgs", "ndbc", "coops"}
PARSE_ERRORS = (KeyError, TypeError, ValueError, IndexError, AttributeError)


class Unreadable(Exception):
    """The reply is not in the format the app expects."""


class Unavailable(Exception):
    """The feed could not be fetched."""


class Client:
    def __init__(self) -> None:
        self._http = httpx.Client(
            headers={"User-Agent": UA, "Accept-Encoding": "gzip"}, timeout=30, follow_redirects=True
        )
        self._last = 0.0

    def get(self, url: str, **kwargs) -> httpx.Response:
        wait = self._last + PAUSE - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            resp = self._http.get(url, **kwargs)
        except httpx.TransportError as exc:
            raise Unavailable(f"{type(exc).__name__}: {exc}") from exc
        finally:
            self._last = time.monotonic()
        code = resp.status_code
        if code in GONE:
            raise Unreadable(f"HTTP {code} for {url}: the feed moved or was removed")
        if code >= 500 or code in DOWN:
            raise Unavailable(f"HTTP {code} for {url}")
        if code >= 400:
            raise Unreadable(f"HTTP {code} for {url}")
        return resp

    def json(self, url: str, **kwargs):
        try:
            return self.get(url, **kwargs).json()
        except ValueError as exc:
            raise Unreadable(f"{url} is not JSON: {exc}") from exc

    def close(self) -> None:
        self._http.close()


def features_of(data) -> list:
    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list):
        raise Unreadable("no features list")
    return features


def check_usgs(c: Client) -> str | None:
    """Two summary feeds through parse_feature (strict: one unreadable feature is a format change), one detail."""
    for name in ("significant_month", "all_day"):
        data = c.json(f"{usgs.FEED_BASE}{name}.geojson")
        features = features_of(data)
        for feature in features:
            try:
                usgs.parse_feature(feature)
            except PARSE_ERRORS as exc:
                fid = feature.get("id") if isinstance(feature, dict) else "?"
                raise Unreadable(f"{name} feature {fid}: {exc!r}") from exc
        if usgs.listing(data) is None and features:
            raise Unreadable(f"{name}: listing() cannot read the feed")
    if not features:
        return None
    detail_url = next((f["properties"]["detail"] for f in features if f["properties"].get("detail")), None)
    if detail_url:
        try:
            usgs.parse_detail(c.json(detail_url))
        except PARSE_ERRORS as exc:
            raise Unreadable(f"detail: {exc!r}") from exc
    return f"{len(features)} quakes in all_day"


def check_nws(c: Client) -> str | None:
    data = c.json(nws.URL, params={"event": nws.EVENTS, "status": "actual"}, headers={"Accept": "application/geo+json"})
    try:
        alerts, skipped = nws.parse_features(data)
    except ValueError as exc:
        raise Unreadable(str(exc)) from exc
    if skipped:
        raise Unreadable(f"{skipped} alert(s) unreadable")
    return f"{len(alerts)} alerts" if alerts else None


def check_tsunamigov(c: Client) -> str | None:
    seen = 0
    for url in tsunamigov.FEEDS:
        try:
            entries = tsunamigov.parse_atom(c.get(url).text)
        except ET.ParseError as exc:
            raise Unreadable(f"{url}: {exc}") from exc
        seen += len(entries)
        if entries and entries[0]["cap_url"]:
            first = entries[0]
            bulletin = c.get(first["bulletin_url"]).text if first["bulletin_url"] else ""
            try:
                tsunamigov.parse_cap(c.get(first["cap_url"]).text, bulletin, first["cap_url"])
            except (ET.ParseError, *PARSE_ERRORS) as exc:
                raise Unreadable(f"{first['cap_url']}: {exc!r}") from exc
    return f"{seen} entries" if seen else None


def check_jma(c: Client) -> str | None:
    entries = c.json(f"{jma.BASE}list.json")
    if not isinstance(entries, list):
        raise Unreadable("list.json is not a list")
    entries = [e for e in entries if isinstance(e, dict) and e.get("json")]
    if not entries:
        return None
    detail = c.json(f"{jma.BASE}{entries[0]['json']}")
    if not isinstance(detail, dict):
        raise Unreadable("detail is not an object")
    try:
        jma.parse_detail(detail, entries[0])
    except PARSE_ERRORS as exc:
        raise Unreadable(f"detail {entries[0]['json']}: {exc!r}") from exc
    return f"{len(entries)} reports"


def check_ndbc(c: Client) -> str | None:
    try:
        stations = ndbc.parse_active_stations(c.get(ndbc.STATIONS_URL).content)
    except (ET.ParseError, *PARSE_ERRORS) as exc:
        raise Unreadable(f"activestations.xml: {exc!r}") from exc
    if not stations:
        raise Unreadable("activestations.xml: no stations read")
    try:
        readings = ndbc.parse_dart(c.get(ndbc.DART_URL.format(station_id="46402")).text, "46402")
    except PARSE_ERRORS as exc:
        raise Unreadable(f"46402.dart: {exc!r}") from exc
    return f"{len(readings)} DART readings" if readings else None


def check_coops(c: Client) -> str | None:
    try:
        stations = coops.parse_stations(c.get(coops.STATIONS_URL).content)
    except PARSE_ERRORS as exc:
        raise Unreadable(f"stations.json: {exc!r}") from exc
    if not stations:
        raise Unreadable("stations.json: no stations read")
    end = datetime.now(UTC)
    params = coops._params("water_level", "9414290", end - timedelta(hours=6), end)
    try:
        readings = coops.parse_water_level(c.get(coops.DATA_URL, params=params).content, "9414290")
    except PARSE_ERRORS as exc:
        raise Unreadable(f"water_level: {exc!r}") from exc
    return f"{len(readings)} readings" if readings else None


CHECKS: dict[str, Callable[[Client], str | None]] = {
    "usgs": check_usgs,
    "nws": check_nws,
    "tsunamigov": check_tsunamigov,
    "jma": check_jma,
    "ndbc": check_ndbc,
    "coops": check_coops,
}


def run_checks(c: Client, checks: dict[str, Callable[[Client], str | None]] = CHECKS) -> dict[str, dict]:
    """One result per feed. A check returns a detail string, or None when the feed parsed but held nothing."""
    results: dict[str, dict] = {}
    for name, check in checks.items():
        try:
            detail = check(c)
            results[name] = {"state": "ok", "detail": detail} if detail else {"state": "empty", "detail": "no entries"}
        except Unavailable as exc:
            results[name] = {"state": "unavailable", "detail": str(exc)}
        except Unreadable as exc:
            results[name] = {"state": "format", "detail": str(exc)}
        except Exception as exc:  # a parser crashing on a reply it did not expect is a format change
            results[name] = {"state": "format", "detail": f"parser crashed: {exc!r}"}
    return results


def decide(results: dict[str, dict], streaks: dict[str, int]) -> tuple[dict[str, int], list[str], list[str]]:
    """New streaks of consecutive unavailable (or, for NEVER_EMPTY feeds, empty) runs; the feeds with a format
    failure; the feeds whose streak has reached STREAK_LIMIT."""
    new = {}
    for name, r in results.items():
        counts = r["state"] == "unavailable" or (r["state"] == "empty" and name in NEVER_EMPTY)
        new[name] = streaks.get(name, 0) + 1 if counts else 0
    fmt = [n for n, r in results.items() if r["state"] == "format"]
    return new, fmt, [n for n, v in new.items() if v >= STREAK_LIMIT]


def markdown(results: dict[str, dict], streaks: dict[str, int], fmt: list[str], stuck: list[str]) -> str:
    lines = [f"Feed check {datetime.now(UTC):%Y-%m-%d %H:%M} UTC", ""]
    lines += ["| Feed | State | Detail | Streak |", "|---|---|---|---|"]
    for name, r in results.items():
        detail = r["detail"].replace("|", "/").replace("\n", " ")[:300]
        lines.append(f"| {name} | {r['state']} | {detail} | {streaks[name]} |")
    lines.append("")
    if fmt:
        lines.append("Format change (the reply no longer parses): " + ", ".join(fmt))
    if stuck:
        lines.append(f"Unavailable or empty {STREAK_LIMIT} runs in a row (not a format change): " + ", ".join(stuck))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", type=Path, required=True, help="streak file, read then rewritten")
    ap.add_argument("--report", type=Path, required=True, help="Markdown report to write")
    ap.add_argument("--output", type=Path, help="file that receives raise=<kind> (GITHUB_OUTPUT)")
    args = ap.parse_args(argv)
    try:
        streaks = json.loads(args.state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        streaks = {}
    client = Client()
    try:
        results = run_checks(client)
    finally:
        client.close()
    streaks, fmt, stuck = decide(results, streaks)
    args.state.write_text(json.dumps(streaks), encoding="utf-8")
    report = markdown(results, streaks, fmt, stuck)
    args.report.write_text(report, encoding="utf-8")
    sys.stdout.write(report)
    if args.output:
        with args.output.open("a", encoding="utf-8") as fh:
            fh.write(f"raise={'format' if fmt else 'unavailable' if stuck else ''}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
