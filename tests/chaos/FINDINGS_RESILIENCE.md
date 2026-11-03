# Chaos phase 1b: resilience findings

Probes: `tests/chaos/test_resilience.py`. Run `python -m pytest tests/chaos -q` (about 110 s). Findings are
`xfail(strict=True)` tests that assert the wanted behaviour; each turns into an XPASS error when fixed, so drop the marker then.
Product code untouched. Not re-reported: `parse_collection` exception escape, NaN/out-of-range lat, `bearing_deg` 360.0.

| # | Proposed | Where | Trigger | Failing test |
|---|----------|-------|---------|--------------|
| R-1 | P3 | `earthwave/sources/usgs.py:256-257` (`fetch`; same at 307 `fetch_range`) | HTTP 200 body `{}`, `{"error":"maintenance"}`, `{"type":"FeatureCollection"}` or `{"features":{"a":1}}`. No `features` list parses to zero quakes and the poll counts as healthy: failures reset to 0, `last_ok` set, source shows FRESH while serving nothing. A wrong-typed `features` (dict) is iterated as keys. Fix: raise when `features` is not a list. | `test_200_without_features_is_a_failure[...]` (4 params) |
| R-2 | P3 | `usgs.py:276-285` (`fetch`, `coverage=feed_coverage(feed)`) | Same `{}` body: the batch still claims `coverage=(hours, floor)`, so `Store.is_covered(168, 4.5)` is true from an empty reply and the view never backfills. Falls out of R-1's fix. | `test_200_without_features_claims_no_coverage` |
| R-3 | P4 | `usgs.py:243-255` (`fetch`, 304 branch) | 304 answering a request that carried no `If-Modified-Since` (misbehaving proxy), on startup feed. Returns a good `not_modified` batch, `_done` pops the startup feed, store stays empty and the source is marked ok. Fix: treat 304 with no `same_feed` validator as an error. | `test_304_to_an_unconditional_request_is_not_a_good_poll` |
| R-4 | P3 | `usgs.py:334` (`fetch_detail`), `parse_nearby_cities` 220-ish | nearby-cities JSON that is a dict, `["x"]`, `[1]`, `null` or a string. The `except (httpx.HTTPError, ValueError)` misses AttributeError/TypeError, so the whole detail (PAGER, ShakeMap) is lost and the screen shows "Detail unavailable: AttributeError" for an optional extra. Fix: widen to `Exception`, or have `parse_nearby_cities` ignore non-dict rows and non-lists. | `test_malformed_nearby_cities_keeps_the_rest_of_the_detail[...]` (5 params) |
| R-5 | P2 | `earthwave/state/store.py:341-343` (`_merge_gauges`) | One gauge reading with a far-future `ts` (year 2036, bad sensor clock or upstream bug): `newest = max(stored)` moves, every real reading older than `newest - 24h` is deleted. Station history and anomaly baseline are gone until the next 24 h accrue. Fix: ignore readings later than now + small skew. | `test_future_gauge_timestamp_does_not_erase_history` |
| R-6 | P3 | `store.py:199` (`merge`) | A quake whose `updated` is far in the future (e.g. 2100) is stored; every later genuine revision has smaller `updated` and is skipped (`continue`), so mag/depth/status are frozen for up to 35 days. Fix: clamp `updated` to the batch's `generated`/`fetched_at`. | `test_future_updated_does_not_freeze_a_quake` |

## Held (no finding)

- 429 with/without Retry-After (seconds, HTTP date, garbage, negative, `nan`, `inf`, `1e400`, 86400), 500/502/503/404, 204, empty,
  HTML, truncated JSON, `null`, `[]`, `features: null`, binary, bad UTF-8, read/connect timeout, connect error, protocol error,
  decoding error: `poll_source` never raises, store is unchanged, back-off at least the requested wait (capped at 300 s,
  `RETRY_AFTER_CAP`), total delay under `BACKOFF_CAP` +10% jitter, recovery emits `SourceRecovered`.
- A 429 window stops the origin being asked (PoliteTransport), then lifts. A hung server is cut by `FETCH_DEADLINE`.
- Startup chain skips dead feeds (502 and 204) and reaches steady. fdsnws backfill: 429 honoured, 400/404 block until `switch()`, limit hit claims no coverage, no leaked tasks.
- Blank `{"features":[]}` with no metadata does not wipe the store ("empty listing proves nothing").
- Soak, 40 simulated days past `RETAIN`, 30% faulty answers, gauge flood, detail stream: store stays within `RETAIN`,
  `GAUGE_MAX_ROWS`, `DETAIL_CACHE_SIZE`, `CACHE_ENTRIES` hold, coverage claims bounded, no stray tasks, client closes.
  `Scheduler.run()` for 12 simulated hours under faults: honours `min_interval`, cancels cleanly, no task leak.
- Pilot: 6 seeded 120-key runs with 4 random resizes (down to 24x6) plus 12 Hypothesis examples: no uncaught exception,
  app stays running, focus never lost; every screen returns to the dashboard on Esc.
- Inconclusive: a metadata-stripped partial listing deleting quakes (reconcile spares the feed edges and the fixture quakes
  sat outside the window); not pursued, would need a window-aligned feed.
