# Changelog

## [0.1.0] - 2026-10-31

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

[0.1.0]: https://github.com/WPTK/earthwave/releases/tag/v0.1.0
