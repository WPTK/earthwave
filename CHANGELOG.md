# Changelog

## [Unreleased]

### Added

- Add a `Ctrl+P` command palette (Textual's, with earthwave's actions added): switch screens, sort, filters, map, refresh, quit, and Set home location, which writes the config file through the same atomic writer as `--save-home`
- Add one HTTP layer for every feed: requests that fail in a way likely to pass (connect errors, timeouts, 500, 502, 503, 504, and a 429 or 503 with a short Retry-After) are tried up to three times, always inside one tsunami or JMA entry's 25 s, and a feed that stays down still shows as failing. Conditional requests use an on-disk cache in the data directory (up to 20 MB, entries kept 7 days, a damaged file is replaced). Uses `hishel` and `stamina`
- Add `--save-home` and `--home-name` to keep a home location in the config file without editing it
- Add `--diagnose`: version, Python, OS, terminal variables, config, history and log paths and feed reachability in one block to paste into a bug report
- Add `EARTHWAVE_MAP_RENDERER` (`auto`, `braille`, `sixel`) to force the map renderer for one run
- Add `EARTHWAVE_CONFIG_DIR` and `EARTHWAVE_DATA_DIR` to move the config file, the history database and the log; on Windows the defaults follow `%LOCALAPPDATA%`

### Fixed

- Fix `--debug --selftest` writing no log, and `--help` not naming the default log file
- Fix feeds being unreachable behind a proxy: `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` and the Windows system proxy are now honoured
- Fix `r` implying a fresh poll: the toast now names the sources still inside their minimum interval and how long until they can be asked again
- Raise the per-request read timeout from 20 s to 30 s so a feed that takes 20 s to answer is not cut off at the limit
- Fix settings errors: each one names its section and key, and all are reported together; syntax errors give the line
- Fix `--selftest` printing SKIPPED in the release binary: the binary now carries the fixtures it parses

## [0.2.0] - 2026-11-01

### Changed

- Draw the map at pixel resolution with sixel graphics when the terminal reports them, with braille as the fallback; `map_renderer` under `[view]` forces either
- Enlarge the markers for magnitudes 3.4 to 5.9
- Redraw the braille map with coast and land on one dot grid, a smoother coast and a light land hatch
- Show the world map at equal scale at any panel size, and zoom through world, wide, region and close views with or without a selected quake

### Added

- Add land fill under the coastline
- Add place labels to the braille map
- Add a magnitude legend and a list of the strongest quakes in the rows left under the world map

### Fixed

- Fix M7+ map markers and text that were nearly invisible under `NO_COLOR`
- Fix quakes published after launch going unannounced when every startup feed failed

## [0.1.0] - 2026-11-01

### Added

- Add a live earthquake list from the USGS feeds with a detail screen (PAGER, nearby cities, links), filters, search, sorting and a status bar showing how fresh each feed is
- Add history backfill from the USGS event service when the filters reach back further than the feeds do
- Add flashing rows for new, updated and major quakes, an unseen counter and an in-app notice for major quakes
- Add tsunami alerts from tsunami.gov, the National Weather Service and the Japan Meteorological Agency, merged into one list that follows watches, warnings, advisories, information statements, follow-ups and cancellations
- Add a tsunami panel, a tsunami screen with the full bulletin text, a coloured banner and status bar for the alert in force, and acknowledgement of alerts
- Add desktop notifications (Windows toast, `notify-send`, `osascript`) and a terminal bell for new alerts and major quakes, and `--notify` to force them on
- Add a world map with Natural Earth 1:10m coastlines in braille or ASCII glyphs, quake and alert markers, zoom and pan
- Add sea-level readings from NOAA DART buoys and CO-OPS tide gauges, with a gauge screen, a gauge strip on the tsunami screen and anomaly alerts
- Add a SQLite history of quakes, alerts and acknowledgements, a day-by-day history screen and `--offline` to browse it without a network
- Add `--record` and `--replay` to save and replay a session of feed responses, and `--speed` to change the replay speed
- Add a commented config file, written on first run, and the options `--config`, `--no-config`, `--min-mag`, `--hours`, `--home`, `--glyphs`, `--log` and `--debug`
- Add a help screen that lists the keys, and a notice when the terminal is too small
- Add one-file binaries for Windows, Linux and macOS, attached to each GitHub release with checksums
- Add `--version` and `--selftest`

[0.2.0]: https://github.com/WPTK/earthwave/releases/tag/v0.2.0
[0.1.0]: https://github.com/WPTK/earthwave/releases/tag/v0.1.0
