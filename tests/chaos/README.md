# Chaos probes

Tests from the chaos audit (Discussion #296). They feed the app hostile or awkward input and assert the behaviour we
want. Fixed findings are ordinary regression tests. A finding that is still open is `xfail(strict=True)` with the
issue number in the reason; when the fix lands the probe XPASSes, which fails the run, and the marker is deleted.

Run: `python -m pytest -q -n auto tests/chaos`. The slow soak and random-key probes carry the `chaos` marker
(registered in `pyproject.toml`), so `-m "not chaos"` skips them. CI runs everything. SVGs written for eyeballing go to
`tests/chaos/out/` (git-ignored), never to `tests/__snapshots__`.

Findings whose probe already exists as a focused test elsewhere (`tests/test_usgs_hardening.py`, `test_store_chaos.py`,
`test_geo_chaos.py`, `test_quake_list_chaos.py`, `test_cvd_palette.py`) are not repeated here.

## `test_phase1_fuzz.py` - parser, store and geodesy

| Probe | Guards | Issue |
|-------|--------|-------|
| `test_fuzz_parse_collection_total` (Hypothesis) | `parse_collection` never raises on arbitrary feature JSON | #284, #285 |
| `test_spec_negative_and_null_fields_parse` | null/negative mag, 2-element coordinates, blasts and ice quakes parse or are skipped | - |
| `test_swarm_with_revisions_no_dupes_no_drops_no_sort_crash` | 300-event swarm with id supersession and stale revisions | - |
| `test_ring_bound_over_simulated_days` | the store stays inside `MAX_VIEW_HOURS` over 50 simulated days | - |
| `test_haversine_vs_geographiclib`, `test_edge_points` | haversine within 0.6% of WGS84; poles, antimeridian and antipodes finite; bearing in [0, 360) | #291 |

## `test_resilience.py` - HTTP faults, soak, random keys

| Probe | Guards | Issue |
|-------|--------|-------|
| `test_fault_poll_survives_backs_off_and_keeps_data` | 429/5xx/404/204/HTML/truncated/binary/timeouts: poll never raises, store unchanged, back-off honours Retry-After, recovery event | - |
| `test_backoff_grows_and_is_capped_over_a_long_outage`, `test_polite_transport_short_circuits_during_a_429_window`, `test_hanging_server_is_cut_by_the_deadline` | back-off cap, politeness window, `FETCH_DEADLINE` | - |
| `test_startup_chain_*`, `test_blank_feature_list_does_not_wipe_the_store` | dead startup feeds are skipped; an empty listing proves nothing | - |
| `test_backfill_*` | fdsnws backfill: 429, 4xx block until `switch()`, limit hit claims no coverage | - |
| `test_detail_*`, `test_nearby_cities_http_failure_degrades` | detail faults raise cleanly, null sections degrade, cache bounded | #289 |
| `test_soak_36_days_caps_hold_under_random_faults` (`chaos`) | store, gauge, detail cache and coverage claims stay bounded with 30% faulty answers | #286, #287 |
| `test_scheduler_run_12_simulated_hours_no_task_or_client_leak` | `Scheduler.run()` honours `min_interval`, cancels cleanly | - |
| `test_random_keys_seeded`, `test_random_keys_hypothesis` (`chaos`) | random key presses and resizes: no uncaught exception, app alive, focus kept | - |
| `test_every_screen_then_escape_returns_to_dashboard_with_focus` | every screen leaves on Esc | - |

Reduced from the audit run (about 110 s) to keep CI fast: soak 40 days at a 10 minute poll became 36 days at 30 minutes
(still past the 35-day `RETAIN`); seeded key runs 6 x 120 keys became 2 x 60; Hypothesis examples 12 x 40 keys became 5 x 30.

## `test_ux.py` - layout, focus, sort stability, colour

| Probe | Guards | Issue |
|-------|--------|-------|
| `test_dashboard_layout`, `test_status_bar_fits_one_line`, `test_detail_screen_layout`, `test_tsunami_screen_layout`, `test_dashboard_has_a_tsunami_banner_*` | nothing leaves the screen at 80x24, 120x40, 200x60; the too-small overlay replaces screens; TSUNAMI always visible in an alert | - |
| row robustness (`test_narrow_row_widths_*`, `test_extreme_depth_*`, `test_future_dated_quake_*`, `test_empty_place_*`, `test_pager_alert_unknown_or_null_*`) | list rows stay aligned at any width, depth, age, place, PAGER value | #292, #294 |
| `test_selected_row_foregrounds_stay_readable_on_the_cursor_background` | every row foreground clears 3:1 on the cursor row (UX-6, was 2.75:1 for red) | #275 covers the wider contrast audit |
| `test_palettes_are_distinguishable_with_normal_vision`, `test_every_colour_coded_field_has_a_non_colour_cue_*`, `test_tsunami_flag_*`, `test_no_color_*` | colour is never the only cue | #293 (palette itself: `tests/test_cvd_palette.py`) |
| `test_cursor_*`, `test_scrolled_away_view_*`, `test_idempotent_poll_*`, `test_selected_quake_dropping_out_*` | selection and scroll survive polls | #295 |
| `test_dashboard_tab_*`, `test_open_search_box_*`, `test_escape_leaves_every_pushed_screen`, `test_tab_reaches_every_focusable_*`, `test_escape_inside_a_filter_input_*`, `test_focus_returns_*`, `test_hidden_map_*`, `test_detail_scroll_area_*` | Tab order and Esc on every screen | - |
| `test_status_bar_keeps_feed_health_visible_at_100_columns` | **xfail**: with an alert and a search active the bar is cut before feed health | #228 |
| `test_escape_leaves_the_fullscreen_map` | **xfail**: Esc does nothing in the full-screen map; only `M` leaves it | #277 |
