# Chaos audit, phase 1: UX / visual / focus findings

Probes: `tests/chaos/test_ux.py` (`python -m pytest tests/chaos/test_ux.py -q`: 71 pass, 21 xfail, about 65 s).
Each finding is a strict `xfail`, so the suite stays green; `--runxfail` shows it failing, and a fix flips it to XPASS,
which fails the run until the marker is deleted. Product code was not touched. SVGs and the CVD table are written to
`tests/chaos/out/` (git-ignored).

Proposed severities are conservative: all are cosmetic or have a non-colour fallback, nothing is P0-P2.

## Findings

| id | proposed | where | summary |
|----|----------|-------|---------|
| UX-6 | P3 | `quake_list.py:22,148,159` | M7+ and PAGER-red text is 2.75:1 on the selected-row background |
| UX-3 | P3 | `status_bar.py:39-70` | with an alert, search and sort active the bar is cut before feed health at 100 cols |
| UX-1 | P4 | `quake_list.py:140-143` | place column is sized in characters, not cells; CJK/emoji overflow, tab/newline pass through |
| UX-2 | P4 | `quake_list.py:21,148` | MAG column is 4 wide; a magnitude of `-10.1` or `>=100` pushes every later column right |
| UX-4 | P4 | `theme.py:16-50` | green and yellow collapse for protanopes; THREAT and INFORMATION share `sky_blue1` |
| UX-5 | P4 | `quake_list.py:143` | PAGER match is case-sensitive: `"Red"` is dropped from the list column |
| UX-7 | P4 | `store.py:253-255`, `store.py:751-756` | tie order follows the latest batch order, not a stable key |
| UX-8 | P4 | `dashboard.py:39,257` | Escape does not leave the full-screen map; only `M`, which is `show=False` |

### UX-6 (P3) red is the least readable colour on the cursor row
- Repro: select any M7+ or PAGER-red row. ANSI red is `#f4005f` under Textual's ANSI theme, on `CURSOR_BG #1e3a5f`:
  2.75:1 (WCAG wants 4.5 for text, 3 for large). The rows that matter most are the hardest to read when selected.
  Every other foreground (cyan, green, yellow, orange, grey62, bold blue) clears 3:1.
- Test: `test_selected_row_foregrounds_stay_readable_on_the_cursor_background`.
- Suggested fix: a brighter red for the selected row, or choose `CURSOR_BG` against the worst foreground.

### UX-3 (P3) status bar hides feed health when crowded
- Repro: `build_status(worst_status(), UNICODE)` with a tsunami alarm, a search, `sort:dist`, a countdown and an unseen
  badge is already 99 characters; the stale/failed phrase after it is clipped by the no-wrap ellipsis at the
  100-column minimum. A plausible live case, not an exotic one. Feed state is the part of the bar that says the data may
  be wrong, and it is appended last.
- Test: `test_status_bar_keeps_feed_health_visible_at_100_columns`.
- Suggested fix: put unhealthy-feed text before the counts, or drop optional fields (sort, countdown) first.

### UX-1 (P4) place column counts characters
- Repro: a quake with `place="東京湾" * 30`, `pager_alert="red"`, `tsunami_flag=True`: the row is 86 cells in a 65-cell
  list; `render_line` crops it and the `TR` flags at the right edge vanish. An emoji place overflows by 2. `"x\ty"` and
  `"a\nb"` reach the strip unchanged (a newline inside a Strip segment corrupts the line).
- Tests: `test_row_width_is_cells_not_characters[cjk-long|emoji|tab|newline]` (xfail); the effect is shown by
  `test_wide_place_effect_is_a_cropped_row_that_loses_the_flags`.
- Impact is low: USGS `place` is ASCII today. A fix would use `rich.cells.cell_len` / `set_cell_size` and strip controls.

### UX-2 (P4) MAG column width
- Repro: `mag=-10.1` or `100.0` gives a field wider than 4 (`f"{mag:4.1f}"`), so the row is too wide and the flags are
  cropped. Real magnitudes (about -2 to 10) fit, so this is academic.
- Test: `test_extreme_magnitude_keeps_columns_aligned`. Depth passes (`-700`, `12345.6`).

### UX-4 (P4) colour-blind safety
- Table (`tests/chaos/out/cvd.txt`, CIE76 dE of the worst pair; below about 20 is hard to tell apart):
  - protanopia: magnitude 4.5-5.4 vs 5.5-6.4 dE 1.8; PAGER green vs yellow dE 1.8
  - deuteranopia: magnitude 4.5-5.4 vs 6.5-6.9 dE 10.2; PAGER green vs orange dE 10.2
  - tritanopia: worst 26.5 (magnitude), 33.5 (PAGER), fine
  - normal vision: worst 37 (magnitude), 47 (PAGER)
  - THREAT and INFORMATION are the same colour (dE 0) in every mode.
- Mitigated: PAGER has the letter G/Y/O/R, the magnitude is printed as a number, level names are text. Colour is
  redundant in the list, detail and tsunami screen (`test_every_colour_coded_field_has_a_non_colour_cue_in_the_row`,
  `test_tsunami_flag_and_pager_in_detail_are_words_not_colour` pass). The residual risk is the map dots and the
  panel/banner borders, where colour is the cue; only the palette was probed there.
- Tests: `test_green_and_yellow_survive_red_green_colour_blindness[protanopia|deuteranopia]`,
  `test_threat_and_information_have_distinct_colours`. Report: `test_cvd_report[*]`.

### UX-5 (P4) PAGER case
- Repro: `pager_alert="Red"` renders blank in the list (`quake_list.py:143` tests `in PAGER_COLORS`); the detail screen
  prints `Red` in white. USGS sends lowercase, so this matters only if a source ever differs. Better fixed by
  normalising in the parser (`usgs.py:119`) than in the widget.
- Test: `test_pager_alert_matching_is_case_insensitive`.

### UX-7 (P4) sort ties are not stable, and the cause is one line
- Root cause: `Store._put` does `self._drop(id)` then `self._quakes[id] = quake`, so every merge of a quake moves it to
  the end of the dict even when nothing changed. `visible()` sorts with no final tiebreak, Python's sort is stable on
  dict order, so ties (same time and magnitude; same coordinates for `dist`) follow the order of the latest batch.
- Repro: six quakes with equal time, magnitude and epicentre; merge the same batch reversed: the visible order flips on
  every re-poll, in all three sorts. A revised quake also jumps to the end of its ties. Two stores that received the
  same data in different orders (restart from history vs live) disagree.
- Reach: needs an exact tie. `time` ties are millisecond-level (rare), `mag` falls back to time, `dist` ties need
  identical coordinates. Hence P4. Fix: add `q.id` as the last sort key (and optionally keep the dict position in `_put`).
- Tests: `test_equal_key_rows_keep_their_order_across_repeated_unchanged_polls[time|mag|dist]`,
  `test_a_revised_quake_does_not_jump_among_its_equals[...]`, `test_equal_key_order_does_not_depend_on_arrival_order[...]`.

### UX-8 (P4) full-screen map exit
- Repro: `M` opens the full-screen map; `Escape` does nothing and Tab has nowhere to go (the list is hidden). `M` is
  `show=False`, so the footer does not name the exit. Not a trap (`M` and `m` work, help lists them) but every other
  screen leaves on Escape.
- Test: `test_escape_leaves_the_fullscreen_map`.

## Checked and fine (passing probes)

- Layout at 80x24, 120x40, 200x60 for dashboard, detail, tsunami screen and status bar: no widget leaves the screen,
  every list strip equals the content width, the overlay replaces (does not overdraw) the screens below 100x30 and Escape
  still leaves detail and tsunami under it, and "TSUNAMI" is visible at every size when an alert is in force
  (`test_dashboard_layout`, `test_detail_screen_layout`, `test_tsunami_screen_layout`, `test_status_bar_fits_one_line`,
  `test_dashboard_has_a_tsunami_banner_at_every_size_when_an_alert_is_in_force`).
- Row robustness: depth `-700` and `12345.6`, a future-dated quake (age clamps to `0s`), empty place (`unknown`),
  header and row width agreement from 0 to 60 columns.
- PAGER `None`, `""`, `"purple"`, `" "` with `tsunami_flag` True/False: the column renders blank or `T`, never wrong,
  and the detail screen prints a PAGER line only for a non-empty value.
- NO_COLOR: every cue survives (age, magnitude digits, `T`, PAGER letter, cursor marker), export renders.
- Sort stability under polls: an unchanged poll leaves rows, cursor and scroll identical; a poll adding three rows above
  and three below keeps the selected quake and its screen line in time, mag and dist sorts; a list scrolled away keeps
  its top row; the selection survives a search that empties and restores the list.
- Focus: Tab and Shift+Tab reach every focusable widget on the dashboard (100x30, 120x40, 200x60) and on the Filters and
  Tsunami screens; an open search box never strands focus and Escape returns it to the list; Escape leaves Help, Filters,
  Tsunami and Detail; Escape in a Filters input cancels without applying; focus returns to the list after a too-small
  round trip; Tab never lands on a hidden map.

## Coverage gaps (existing tests)

- The dashboard has one snapshot (`test_ui.py:572`, 120x40). 80x24 is covered only as the too-small overlay
  (`tests/uat`), 200x60 has none; the map alone has 160x40 and 240x60 snapshots.
- The detail screen has no snapshot at any size; the status bar has none (only `build_status` text tests).
- The tsunami screen has snapshots only at 120x40.
- Nothing pins the contrast of a row foreground against `CURSOR_BG` (UX-6) or colour-blind separation (UX-4).
- Not covered by this pass: Observations and History screens (History needs a saved-history db), mouse focus, the
  full-screen map's own layout, and real-terminal rendering (SVG export only).
