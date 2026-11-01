# earthwave

An earthquake and tsunami tracker for the terminal. It follows free, keyless public feeds, with USGS as the backbone,
and is written in Python 3.13 with [Textual](https://textual.textualize.io/).

![earthwave replaying a recorded Kamchatka earthquake: a tsunami watch, then a warning, then the quake in the list and on the map](docs/img/demo.svg)

*Recorded data, not live: the app replaying `tests/fixtures/replay/kamchatka_watch_to_warning.jsonl` with
`--replay`. Re-record it with `python scripts/record_demo.py`.*

**Status: pre-release (0.0.x).** Milestones M1 to M6 are in progress. Follow them on the
[milestones](https://github.com/WPTK/earthwave/milestones) page and the
[issue tracker](https://github.com/WPTK/earthwave/issues). The design is in [`docs/plan.md`](docs/plan.md).

## Planned features

- Live USGS earthquakes, with new and major events flashing in the list
- Tsunami alerts from tsunami.gov, the National Weather Service and the Japan Meteorological Agency
- DART buoy and CO-OPS tide-gauge readings to check an alert against real sea-level data
- A braille world map showing where events are happening
- Desktop notifications for major quakes and tsunami warnings
- SQLite history that persists across runs
- Record a session and replay it later, for demos and tests

None of these are released yet.

## What works today

The project has only been bootstrapped: the package, the command line and live-captured test fixtures.

```powershell
python -m earthwave --version
python -m earthwave --selftest
```

`--version` prints the version. `--selftest` runs the built-in self-check.

## Development

Requires Python 3.13. On Windows:

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

Checks:

```powershell
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m vulture earthwave scripts --min-confidence 80
.venv\Scripts\python -m pytest -n auto
.venv\Scripts\python -m earthwave --selftest
```

`scripts\capture_fixtures.py` re-captures the saved live payloads under `tests/fixtures/`. Do not reformat those files.
Contributor rules and the milestone workflow are in [`CLAUDE.md`](CLAUDE.md) and [`docs/plan.md`](docs/plan.md).

## Data sources

- [USGS](https://earthquake.usgs.gov/) earthquake feeds and event service
- [NOAA](https://www.noaa.gov/): tsunami.gov bulletins, National Weather Service alerts, NDBC DART buoys and CO-OPS
  tide gauges
- [Japan Meteorological Agency](https://www.jma.go.jp/) (JMA) tsunami information, used with attribution
- [Natural Earth](https://www.naturalearthdata.com/) coastlines, public domain

earthwave is not an official tsunami or earthquake warning service and is not affiliated with any of these agencies.
Do not rely on it for safety decisions; follow your local authorities.

It identifies itself to every feed (`earthwave/<version>` with the repository URL, plus `[notify] contact` if you set
one), asks the feed whether a copy has changed (ETag, Last-Modified) instead of downloading it again, and after a 429 or 503
leaves that address alone for as long as its `Retry-After` asks, up to five minutes.

## Versioning and changelog

Versions follow ZeroVer: the major version stays 0, minor releases add features and patch releases fix bugs. The
changelog follows [Common Changelog](https://common-changelog.org/) and is in [`CHANGELOG.md`](CHANGELOG.md); it has no
entries until the first release.

## License

[MIT](LICENSE)
