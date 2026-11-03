# Phase 1 findings (draft, nothing filed). Repro: `python -m pytest tests/chaos -q`

Result: 28 tests, 17 pass, 11 fail. The 11 failures reduce to 3 root causes.

## F1 (proposed P1) - one malformed feature fails the whole feed batch
`earthwave/sources/usgs.py:100-134`. `parse_feature` has `assert time is not None` (l.101) and calls `.get` on
`properties` / `.split` on `ids` (l.57) / `datetime.fromtimestamp` (l.54). `parse_collection` (l.135-147) catches only
`KeyError, TypeError, ValueError, IndexError`, so `AssertionError`, `AttributeError`, `OverflowError`, `OSError` escape.
`fetch` re-raises, so every poll fails while the bad feature stays in the feed; good neighbours are lost.
Triggers: `time: null`, `time: 1e20`, `updated: 1e20`, `properties: null`, `ids: [..]` or a number.
Tests: `test_one_bad_feature_never_raises[*]` (5 fail), `test_fuzz_parse_collection_total`.
Root fix: catch per-feature `Exception`, drop the `assert`.

## F2 (proposed P2) - non-finite / out-of-range numbers accepted
`usgs.py:104-110`. Python `json` accepts `NaN`/`Infinity`; `float()` passes them and lat 123 passes. They reach
`Store.visible` sorts (`store.py:751-756`: NaN mag/lat gives undefined order, comparisons with `nan` are all False)
and `haversine_km`. Tests: `test_non_finite_values_do_not_enter_store[*]` (4 fail).
Root fix: validate finiteness and |lat|<=90, |lon|<=180 in `parse_feature`; return None.

## F3 (proposed P4) - `bearing_deg` can return 360.0
`earthwave/sources/geo.py:41`. `x % 360.0` of a tiny negative float rounds to 360.0, breaking the documented `[0, 360)`.
Repro `bearing_deg(90, 0, 90, 180)`. Test `test_edge_points[p1]`. `compass()` is unaffected (`% 16`).

## Passed (no defect found)
- `mag: null`, negative mag, negative depth, 2-element coordinates, null alert/cdi/felt, quarry blast / ice quake /
  explosion / volcanic eruption all parse or are skipped correctly.
- 300-event swarm with regional->`us` id supersession, late stale revisions, duplicates: zero duplicates, zero drops,
  stale revision never overwrites, all three sorts survive.
- Retention: store bounded by 35 days (`MAX_VIEW_HOURS`) over 50 simulated days.
- `haversine_km` within 0.6% of geographiclib WGS84 on 500 random pairs; poles, +-180 and antipodes finite; asin clamp present.

## Premise corrections to the mandate
- The main quake list is a custom `ScrollView` (`ui/widgets/quake_list.py`), not a `DataTable`; the only
  `table.clear()` on polls is the tsunami screen (`ui/screens/tsunami.py:136`). The in-place sync item shrinks to that.
- No P/S-wave, energy, or aftershock-clustering code exists (features, not bugs). No stale/OFFLINE banner beyond the
  `Freshness` words in `ui/widgets/status_bar.py:14`.
- Existing snapshots cover 120x40 (80), 80x24 (9), 200x50 (4); 200x60 and 120x40 for most screens not systematic.

## Not covered in phase 1
obspy.taup (not installed, heavy; no wave-math code to compare yet), SeedLink/FDSNWS live behaviour, HTTP 429/502/503
fault injection, Textual Pilot state-machine monkey test, snapshot diffs at 3 sizes, PAGER/tsunami badge audit.
