# Configuration

The first run writes a commented `config.toml` to the config directory (paths below). Edit it and restart; the app only
reads it at start. A mistake is reported with the section and key it is in, and every mistake is listed at once.
`--config PATH` reads another file, and `--no-config` uses the defaults.

## Keys and ranges

Values outside a range are rejected. Choices are listed in the file's own comments.

| Section | Key | Default | Allowed |
| --- | --- | --- | --- |
| `[poll]` | `usgs_seconds` | 60 | 60 to 3600 |
| | `tsunami_seconds` | 60 | 30 to 600 |
| | `dart_seconds` | 900 | 60 to 3600 |
| | `coops_seconds` | 300 | 60 to 3600 |
| | `alert_gauge_seconds` | 60 | 30 to 600 |
| `[view]` | `min_magnitude` | 2.5 | -2 to 10 |
| | `hours` | 24 | 1 to 840 (35 days; the store keeps no more) |
| | `glyphs` | `"auto"` | `auto`, `unicode`, `ascii` |
| | `map` | `true` | `true`, `false` |
| | `map_renderer` | `"auto"` | `auto`, `braille`, `sixel` |
| | `time_zone` | `"local"` | `local`, `UTC` |
| `[home]` | `name` | `""` | empty means no home and no distance column |
| | `lat`, `lon` | 0.0 | -90 to 90, -180 to 180 |
| | `radius_km` | 500 | 1 to 20000 |
| `[alerts]` | `major_magnitude` | 6.0 | 0 to 10 |
| | `near_home_magnitude` | 3.0 | -2 to 10 |
| | `pager_min` | `"yellow"` | `green`, `yellow`, `orange`, `red` |
| | `tsunami` | `true` | `true`, `false` |
| | `gauge_anomaly_dart_m` | 0.05 | 0.001 to 10 |
| | `gauge_anomaly_gauge_m` | 0.3 | 0.001 to 10 |
| `[notify]` | `desktop`, `bell` | `true` | `true`, `false` |
| | `contact` | `""` | ASCII text appended to the User-Agent sent to NOAA and NWS |
| `[sources]` | `usgs`, `tsunamigov`, `nws`, `jma` | `true` | `true`, `false` |
| `[history]` | `retain_days` | 90 | 1 to 3650 |

## Home location

`--home 40.7,-74.0` applies to that run only. This writes it to the config file and exits, leaving the rest of the
file as it was:

```
earthwave --home 40.7,-74.0 --home-name "New York" --save-home
```

In the app, `Ctrl+P` then "Set home location" takes `LAT,LON` and an optional name and saves them to the config file;
restart earthwave to use them. Or edit the file:

```toml
[home]
name = "New York"
lat = 40.7
lon = -74.0
```

## Where files live

| | Windows | Linux | macOS |
| --- | --- | --- | --- |
| Config | `%LOCALAPPDATA%\earthwave\config.toml` | `~/.config/earthwave/config.toml` | `~/Library/Application Support/earthwave/config.toml` |
| Log (only with `--log` or `--debug`) | `%LOCALAPPDATA%\earthwave\Logs\earthwave.log` | `~/.local/state/earthwave/log/earthwave.log` | `~/Library/Logs/earthwave/earthwave.log` |
| HTTP cache | `%LOCALAPPDATA%\earthwave\cache\http.sqlite` | `~/.local/share/earthwave/cache/http.sqlite` | `~/Library/Application Support/earthwave/cache/http.sqlite` |
| Saved history | `%LOCALAPPDATA%\earthwave\history.sqlite` | `~/.local/share/earthwave/history.sqlite` | `~/Library/Application Support/earthwave/history.sqlite` |

On Windows `%LOCALAPPDATA%` is read directly. On Linux and macOS the paths come from
[platformdirs](https://platformdirs.readthedocs.io/), and the `XDG_*` variables move the Linux ones. Uninstalling
earthwave never deletes these directories.

## Environment variables

| Variable | Effect |
| --- | --- |
| `EARTHWAVE_CONFIG_DIR` | directory holding `config.toml`; wins on every OS |
| `EARTHWAVE_DATA_DIR` | directory holding `history.sqlite`, the HTTP cache under `cache/` and the log under `logs/`; wins on every OS |
| `EARTHWAVE_MAP_RENDERER` | `auto`, `braille` or `sixel` for one run; wins over `map_renderer` |
| `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`, `NO_PROXY` | proxy for feed requests |

## Proxies

Requests use the proxy variables above and, on Windows, the system proxy setting. A scheme-specific variable
(`HTTPS_PROXY` for an https URL) is used before `ALL_PROXY`, and a host matched by `NO_PROXY` goes direct. A setting
that cannot be used is logged once and skipped, so a typo costs the proxy, not every feed.

## Map renderer

On a terminal that supports sixel graphics the map is drawn at pixel resolution; otherwise it is drawn in braille.
`map_renderer` under `[view]` is `auto` by default: sixel when the terminal reports it, braille otherwise (also under
`NO_COLOR`, `TERM=dumb`, tmux or screen, a pipe, or a terminal that does not answer). `braille` or `sixel` forces either.
Terminals reported to support sixel include Windows Terminal 1.22 or later, foot, WezTerm and xterm built with sixel;
this has not been checked for every release.
