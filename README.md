# earthwave

earthwave is a terminal app for following earthquakes and tsunami alerts. It shows live USGS earthquakes in a list, on
a world map and in a detail screen, and it watches the tsunami warning centres and sea-level gauges so you can see an
alert next to what the water is actually doing. It uses only free public feeds that need no key or account.

![earthwave replaying a recorded Kamchatka earthquake: a tsunami watch, then a warning, then the quake in the list and on the map](https://raw.githubusercontent.com/WPTK/earthwave/main/docs/img/demo.svg)

*Recorded data, not live: the app replaying a saved session with `--replay`.*

earthwave uses 0.x version numbers: details such as key bindings and the config file may change between minor
versions. See [`CHANGELOG.md`](https://github.com/WPTK/earthwave/blob/main/CHANGELOG.md).

## Install

Pick one. The first needs Python 3.12 or newer; the binary needs none. earthwave is on
[PyPI](https://pypi.org/project/earthwave/).

**From PyPI.** uv fetches Python 3.12 if you lack it. On an older Python, pip refuses with
`requires a different Python`; use uv or the binary.

```
pipx install earthwave
uv tool install earthwave
python -m pip install earthwave     # inside a virtual environment
```

To upgrade, run `pipx upgrade earthwave` or `uv tool upgrade earthwave`. To remove it, run `pipx uninstall earthwave`
or `uv tool uninstall earthwave`.

**From source (development version).** This builds `main`, which can be ahead of the latest release. To pin a release
instead, replace `TAG` with its tag from [Releases](https://github.com/WPTK/earthwave/releases).

```
pipx install git+https://github.com/WPTK/earthwave.git@TAG
```

**Release binary.** Download the file for your system from the latest
[release](https://github.com/WPTK/earthwave/releases) (names carry the tag, shown here as `TAG`):

- Windows: `earthwave-TAG-windows-x86_64.exe`. Save it in a folder on your `PATH` and rename it to `earthwave.exe`.
- Linux x86-64: `earthwave-TAG-linux-x86_64.tar.gz`. Unpack it and put `earthwave` on your `PATH`:
  `tar -xzf earthwave-TAG-linux-x86_64.tar.gz && mkdir -p ~/.local/bin && install -m 755 earthwave ~/.local/bin/`.
  It needs glibc 2.35 or newer (Ubuntu 22.04 and later); musl systems such as Alpine are not supported.
- macOS arm64: `earthwave-TAG-macos-arm64.tar.gz`, installed the same way (`~/.local/bin` is not on the macOS `PATH`
  by default). To clear the Gatekeeper prompt: `xattr -d com.apple.quarantine earthwave`.

The binaries are not code-signed, so Windows SmartScreen and macOS Gatekeeper will ask you to confirm. Check the
download against `SHA256SUMS` from the same release. The platforms CI covers are listed in
[`docs/development.md`](https://github.com/WPTK/earthwave/blob/main/docs/development.md#platforms); older Windows versions and the legacy console host are not tested.

**Check the install** with `earthwave --version` and `earthwave --selftest`. The self-test imports five dependencies,
parses the bundled sample feeds, renders one screen without a terminal, prints `selftest: ok` and exits 0. It does not
use the network.

## Run

```
earthwave
```

The first run writes a commented config file to your user config directory. Use a terminal at least 100 columns by 30
rows; below that the app asks you to enlarge the window. The map appears at 120 columns or more.

For example, `earthwave --min-mag 4.5 --hours 72 --home 40.7,-74.0`.

| Flag | Meaning |
| --- | --- |
| `--config PATH` | read this config file instead of the default |
| `--no-config` | ignore the config file and use defaults |
| `--min-mag M`, `--hours N` | smallest magnitude and hours back to list |
| `--home LAT,LON`, `--home-name NAME` | home location for this run, and its name |
| `--save-home` | write `--home` and `--home-name` to the config file, then exit |
| `--glyphs auto\|unicode\|ascii` | glyph set; `ascii` for terminals without braille |
| `--offline` | fetch nothing; browse the saved history |
| `--record PATH`, `--replay PATH` | save every HTTP exchange; serve a recording instead of the network |
| `--speed N` | replay speed multiplier, default 1 (with `--replay`) |
| `--notify` | force toasts and the bell on (they are off under `--replay`) |
| `--log PATH` | write a rotating log file to PATH (WARNING and above) |
| `--debug` | DEBUG logging to the default log file (`--help` prints the path) |
| `--diagnose` | print version, platform, terminal, file paths and feed reachability, then exit |
| `--version`, `--selftest` | see "Check the install" |

Config keys, home location, file paths, environment variables, proxies and the map renderer are in
[`docs/configuration.md`](https://github.com/WPTK/earthwave/blob/main/docs/configuration.md). Aftershock grouping, tsunami banners and notifications, replay and the
saved history are in [`docs/features.md`](https://github.com/WPTK/earthwave/blob/main/docs/features.md). Setup, checks and releases are in
[`docs/development.md`](https://github.com/WPTK/earthwave/blob/main/docs/development.md).

## Keys

`?` opens the full list. The main ones:

| Key | Action |
| --- | --- |
| `j` `k`, arrows | move in the list |
| `Enter` | quake detail (`b` opens the USGS page, `y` copies its URL) |
| `f` `s` `/` | filters, sort, search |
| `g` `Space` | group aftershocks under their mainshock; open or close the group under the cursor |
| `m` `z` | show or hide the map, zoom it |
| click a dot | select that quake in the list (opening its aftershock group if it is folded) |
| `t` `o` `h` | tsunami alerts and bulletins; sea-level gauges; history by day |
| `a` `r` | acknowledge alerts; refresh now |
| `Ctrl+P` | command palette: every action by name, and Set home location |
| `q` | quit |

## Data sources

- [USGS](https://earthquake.usgs.gov/) earthquake feeds and event service
- [NOAA](https://www.noaa.gov/): tsunami.gov bulletins, National Weather Service alerts, NDBC DART buoys and CO-OPS
  tide gauges
- [Japan Meteorological Agency](https://www.jma.go.jp/) tsunami information, used with attribution
- [Natural Earth](https://www.naturalearthdata.com/) coastlines and land polygons, public domain

earthwave identifies itself to every feed, asks whether a copy has changed instead of downloading it again, and backs
off when a feed says to.

**earthwave is not an official tsunami or earthquake warning service and is not affiliated with any of these agencies.
Do not rely on it for safety decisions; follow your local authorities.**

## Reporting a bug

Run `earthwave --diagnose` and paste its output into an issue at
[github.com/WPTK/earthwave/issues](https://github.com/WPTK/earthwave/issues). If the problem can be reproduced, run
`earthwave --debug` and add the end of the log.

## Licence

[MIT](https://github.com/WPTK/earthwave/blob/main/LICENSE)

The release binary bundles third-party libraries under their own licences: hishel and anysqlite (BSD-3-Clause), stamina,
Textual and Rich (MIT), tenacity and msgpack (Apache-2.0), and httpx (BSD-3-Clause). The licence texts of these
libraries are kept in their `*.dist-info` directories inside the binary; the remaining dependencies are not covered by
this note.
