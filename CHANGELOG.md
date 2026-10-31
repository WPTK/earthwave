# Changelog

## [0.1.0] - 2026-10-31

_First release._

### Added

- Add a live earthquake dashboard from the USGS feeds, with a quake list, detail screen (PAGER and nearby cities), filters, search, sorting and a status bar showing per-source freshness ([#18](https://github.com/WPTK/earthwave/pull/18))
- Add `fdsnws` history backfill when the filters reach beyond what the feeds cover ([#18](https://github.com/WPTK/earthwave/pull/18))
- Add flashing rows for new, updated and major quakes, an unseen badge and an in-app toast for major quakes ([#18](https://github.com/WPTK/earthwave/pull/18))
- Add tsunami alerts from tsunami.gov, the NWS API and JMA, merged into one list with a panel, a tsunami screen, a red banner and status bar alert states ([#21](https://github.com/WPTK/earthwave/pull/21))
- Add desktop notifications (Windows toast, `notify-send`, `osascript`) and a terminal bell for new alerts, with `--notify` to force them on ([#21](https://github.com/WPTK/earthwave/pull/21))
- Add a world map with Natural Earth 1:10m coastlines in braille or ASCII glyphs, quake markers, zoom and pan ([#15](https://github.com/WPTK/earthwave/pull/15), [#19](https://github.com/WPTK/earthwave/pull/19))
- Add sea-level observations from NOAA DART buoys and CO-OPS tide gauges, with a gauge strip on the tsunami screen and anomaly alerts ([#17](https://github.com/WPTK/earthwave/pull/17), [#20](https://github.com/WPTK/earthwave/pull/20), [#22](https://github.com/WPTK/earthwave/pull/22))
- Add a SQLite history of quakes and acknowledgements, a day-by-day history screen and `--offline` to browse it without network ([#37](https://github.com/WPTK/earthwave/pull/37))
- Add `--record` and `--replay` to capture and replay HTTP sessions on a replay clock ([#18](https://github.com/WPTK/earthwave/pull/18))
- Add a commented first-run config file and `--config`, `--no-config`, `--min-mag`, `--hours`, `--home`, `--glyphs`, `--log` and `--debug` ([#18](https://github.com/WPTK/earthwave/pull/18))
- Add one-file binaries for Windows, Linux and macOS, built from `earthwave.spec` and attached to each release ([#41](https://github.com/WPTK/earthwave/pull/41))
- Add `--version` and `--selftest` ([#1](https://github.com/WPTK/earthwave/pull/1))

[0.1.0]: https://github.com/WPTK/earthwave/releases/tag/v0.1.0
