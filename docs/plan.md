# earthwave: earthquake and tsunami TUI

## Context

Bill wants a terminal app that tracks earthquakes and tsunamis worldwide from free, keyless public feeds, with USGS as the backbone. It should feel alive: new events flash and change color as they arrive, big ones raise a desktop toast, a terminal map shows where things are happening, and tsunami alerts can be checked against real buoy and tide-gauge readings. History persists across runs, and recorded sessions can be replayed for demos and tests.

Decisions already made (Oct 31 2026):

- Stack: Python + Textual, mirroring the conventions of the existing `WPTK/cfbtui` repo (Textual 8.x, httpx, platformdirs, TOML config under `%LOCALAPPDATA%`, pytest + respx + saved fixtures, ruff, vulture, PyInstaller one-file exe, `CLAUDE.md`, GitHub Actions CI on three OSes).
- Python 3.13, like cfbtui. Python 3.13 is installed on the build machine (Gilead), so no install step is needed.
- Location and name: `C:\earthwave`, package and command `earthwave`.
- v1 must-haves beyond the core list/detail/filters/tsunami panel: in-app live notifications (flashing, color changes), terminal world map, desktop notifications, tsunami observations (DART buoys, tide gauges), history and replay.

Deviations from cfbtui, on purpose: ZeroVer versioning (major stays 0 forever; minor for features, patch for fixes; no FibVer bump tool), Common Changelog format for `CHANGELOG.md`, no self-updater in v1, SQLite added for history, no `tzdata` (only local/UTC display), no `pyperclip` (Textual's OSC 52 clipboard). A GitHub repo under WPTK was not chosen in the scoping answers, so it is an ask-first step at the end of M0, not an assumption.

No Claude attribution anywhere: commit messages carry no `Co-Authored-By` trailer, PR descriptions carry no "Generated with Claude Code" footer, and README, docs, changelog, code comments and release notes never say the project was created, generated or authored by Claude or any AI. This rule is recorded in `CLAUDE.md` in M0 so it applies in every session and takes precedence over any default attribution the tooling suggests.

Execution model: every milestone runs in its own fresh session to keep context windows small. This planning session only produces the plan and starts the M0 session. The handoff between sessions is the repo itself: `docs/plan.md` (a copy of this plan, committed in M0 and kept current) and `CLAUDE.md`. See "Execution: one session per milestone" below.

Cut from v1 after design review: EMSC websocket (second push path plus cross-source dedupe for little value), IOC gauges (registration may be enforced), GDACS (no usable tsunami data), the Settings screen (config file and CLI flags suffice), the tsunami.gov "latest CAP" files (redundant with the Atom entries).

## Data sources (verified live on 2026-10-31)

### Earthquakes

| Source | Endpoint | Notes |
|---|---|---|
| USGS summary feeds (primary) | `https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/{all,1.0,2.5,4.5,significant}_{hour,day,week,month}.geojson` | GeoJSON, refreshed every minute, `Cache-Control: max-age=60`, `Last-Modified` present, no ETag. `If-Modified-Since` returns 304 with an empty body, so polling at 60 s is cheap. Properties: `mag place time updated tz url detail felt cdi mmi alert status tsunami sig net code ids sources types nst dmin rms gap magType type title`; geometry `[lon, lat, depth_km]`; `time`/`updated` epoch ms; `ids`/`sources`/`types` comma-wrapped (`",ci123,"`). USGS docs say automated clients should use these feeds. |
| USGS FDSN event service (history, filters) | `https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson&...` | Same feature shape. Params: `starttime endtime updatedafter minmagnitude maxmagnitude latitude longitude maxradiuskm minlatitude maxlatitude minlongitude maxlongitude orderby(time|time-asc|magnitude|magnitude-asc) limit(<=20000) offset eventid alertlevel mindepth maxdepth reviewstatus minsig`. `count` returns `{"count":n,"maxAllowed":20000}`. 400 above 20000 results; `nodata=404` optional. |
| USGS event detail | the feature's `detail` URL (`/earthquakes/feed/v1.0/detail/{id}.geojson`) | Adds `properties.products`: `origin`, `losspager` (`alertlevel`, `maxmmi`), `shakemap` (`maxmmi`, `maxpga`, `maxpgv`), `dyfi` (`maxmmi`, `num-responses`), `nearby-cities`, `moment-tensor`, `ground-failure`, `phase-data`. All product property values are strings. Fetched lazily when a detail screen opens, LRU-cached (64), never per feed row. |
| EMSC Seismic Portal (post-v1) | `https://www.seismicportal.eu/fdsnws/event/1/query?format=json&limit=N&minmag=M`; push `wss://www.seismicportal.eu/standing_order/websocket` | GeoJSON; props `unid source_id time(ISO) lat lon depth mag magtype flynn_region auth lastupdate`; geometry depth is sign-flipped. HEAD returns 405, use GET. CC BY 4.0. |
| GeoNet NZ, JMA quake list (post-v1) | `https://api.geonet.org.nz/quake?MMI=3`; `https://www.jma.go.jp/bosai/quake/data/list.json` | Both live and keyless; regional intensity data. |
| IRIS / EarthScope event service | retired (404) | Skip. |

### Tsunami alerts and observations

| Source | Endpoint | Notes |
|---|---|---|
| tsunami.gov Atom feeds (primary alerts) | `https://www.tsunami.gov/events/xml/PAAQAtom.xml` (NTWC: US and Canada coasts) and `.../PHEBAtom.xml` (PTWC: Pacific basin, Caribbean, international threat messages) | Atom 1.0 with `geo:lat`/`geo:long`. Entry: `title updated id(urn:uuid) summary(xhtml)` plus two links: `rel="related" type="application/cap+xml"` (that product's CAP) and `rel="alternate"` (plain-text bulletin). Follow the CAP link for every unseen entry id. Quirk: PHEB's `rel=self` points at PAAQ; ignore it. On Oct 31 both feeds held one entry, the Tsunami Information Statement for the M7.0 Bonin Islands quake of Oct 25. |
| tsunami.gov CAP (per product) | linked from each Atom entry | CAP 1.2, signed, namespaced. Fields: `status msgType(Alert|Update|Cancel) event("Tsunami Information", "Tsunami Warning", ...) severity urgency certainty sent expires references`, `area/areaDesc` + `circle` (no polygons seen). `parameter`s: `EventLocationName EventPreliminaryMagnitude EventPreliminaryMagnitudeType EventOriginTime EventDepth EventLatLon`, plus `NWSUGC` (PTWC) or a `WEAK53`-style product id (NTWC). Product URLs follow `/events/{PAAQ|PHEB}/YYYY/MM/DD/{eventid}/{seq}/{WMOhdr}/...`; the directory has no index (403). |
| NWS API alerts (US, structured) | `https://api.weather.gov/alerts/active?event=Tsunami%20Warning,Tsunami%20Advisory,Tsunami%20Watch` | GeoJSON. The only tsunami event types are those three (no information statements). Properties: `id areaDesc geocode{SAME,UGC} affectedZones references sent effective onset expires ends status messageType severity certainty urgency event headline description instruction response parameters(WMOidentifier, AWIPSidentifier)`; `geometry` can be null. A User-Agent naming the app and a contact is required; 429 means retry after about 5 s; `limit` ≤ 400; US states, territories and marine zones only. |
| JMA tsunami (NW Pacific gap filler) | `https://www.jma.go.jp/bosai/tsunami/data/list.json`, then the per-event `json` file in the same folder | Array of `{eid ctt rdt ttl en_ttl at anm en_anm mag kind[{code,kind}] json}`. Detail: `Head{EventID InfoType Serial Headline{Text}}`, `Body{Tsunami{Forecast{Item[{Area{Name,Code} Category{Kind{Name,Code},LastKind} MaxHeight{TsunamiHeight}}]}} Earthquake Comments}`. Undocumented endpoint, Japanese area names, 60 s cache with ETag. The `Kind.Code` → level table is hard-coded from JMA's published XML format spec, not learned from quiet-day fixtures. JMA's terms require attribution. |
| USGS `tsunami` flag | feed property | 1 when USGS links a tsunami product. Tags the quake and starts observation polling; never an alert on its own. |
| NDBC DART buoys | `https://www.ndbc.noaa.gov/data/realtime2/{id}.dart`; station list `https://www.ndbc.noaa.gov/activestations.xml` (`<station id lat lon name type="dart" dart="y">`), `data/stations/station_table.txt` as fallback | Plain text, newest first, about 45 days: `#YY MM DD hh mm ss T HEIGHT`, HEIGHT = water column height in meters, `T` 1 = 15-min standard, 2 = 1-min and 3 = 15-s event mode. Standard-mode data reaches NDBC in 6-hour batches, so the strip is coarse until a buoy enters event mode. About 45 active stations. |
| NOAA CO-OPS tide gauges (US) | `https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?product={water_level|one_minute_water_level|predictions}&station=ID&datum=MLLW&units=metric&time_zone=gmt&format=json&{date=latest|range=N}` | `water_level` is 6-min with rows `{t v s f q}` (quality flags); `one_minute_water_level` rows are `{t v}`; `datum` is mandatory; 1-min data at most 4 days per request; `date=latest` is the last point within 18 min. Stations: `https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json?type=waterlevels` (`id name lat lng state`). No numeric rate limit; spread requests. Confirm `predictions&interval=1` during M4. |
| IOC Sea Level Monitoring (post-v1) | `https://www.ioc-sealevelmonitoring.org/service.php?query=data&code={code}&timestart=...&format=json` | Works without a key today, but registration is stated as required and the v2 API needs a key. Deferred. |
| GDACS, `ptwc.weather.gov` | | GDACS returned no tsunami events; the PTWC host does not resolve. Skip both. |

Level mapping, one `TsunamiAlert.level` across sources: an `event`/title containing Warning → `WARNING`, Advisory → `ADVISORY`, Watch → `WATCH`, Threat → `THREAT`, Information → `INFORMATION`; CAP `msgType=Cancel`, NWS `messageType=cancel` or "Cancellation" in the title → `CANCELLATION`. JMA codes map the same way (major tsunami warning → `WARNING`, forecast of slight sea-level change → `INFORMATION`). An alert stays active until `expires` passes on the injected clock or a cancellation references it. Dedupe key across tsunami.gov and NWS: WMO header + `sent` (NWS `parameters.WMOidentifier`, CAP product id); fallback is source quake + areas, higher level wins. CAP `EventLatLon` and `EventOriginTime` link an alert to its USGS quake (Δt ≤ 5 min and ≤ 200 km). Information statements are the common product and render blue; they never trigger the red banner or a toast.

### Polling etiquette (applies to every source)

- One shared `httpx.AsyncClient` built in `net.py`, `User-Agent: earthwave/<version> (+https://github.com/WPTK/earthwave; <notify.contact>)`, gzip accepted, connect timeout 10 s, read timeout 20 s, closed in the worker's `finally`.
- `If-Modified-Since` on USGS feeds; treat 304 as "fresh, no change". Never poll USGS faster than 60 s.
- Per-source exponential backoff (×2 per consecutive failure, capped at 10 min, ±10% jitter). A failing source never blocks the others.
- All timestamps are aware UTC `datetime`; display local or UTC per config via `astimezone()` only.

## Architecture

Same shape as cfbtui (`espn/` → `sources/`, `state/`, `ui/`, `app.py`, `poll.py`, `config.py`), plus `notify/`, `net.py`, `clock.py` and a bundled `data/` folder.

```
earthwave/
  __init__.py            __version__ = "0.1.0"
  __main__.py            CLI (argparse): --config PATH, --no-config, --min-mag, --hours, --home LAT,LON,
                         --glyphs auto|unicode|ascii, --record PATH, --replay PATH [--speed N], --offline,
                         --log PATH, --debug, --notify (force toasts on under replay), --selftest, --version
  app.py                 EarthwaveApp(config, store=None, clock=None, run_scheduler=True): screens,
                         bindings, the scheduler worker, PollDone fan-out, 0.5 s attention tick
  clock.py               Clock protocol (now(), monotonic()); SystemClock; ReplayClock driven by recorded ts × speed
  net.py                 AsyncClient factory; RecordingTransport (tees {ts, method, url, status, headers, body}
                         to JSONL); ReplayTransport (serves by URL + sequence, advances ReplayClock)
  config.py              TOML load/save, defaults, validation, platformdirs paths, first-run commented template
  poll.py                Multi-source scheduler + Freshness model (port of cfbtui poll.py, generalized to N
                         sources with their own intervals, backoff, generation counter)
  models.py              frozen slotted dataclasses: Earthquake, TsunamiAlert, GaugeReading, Station, Batch
  sources/
    base.py              Source protocol (name, interval(state), fetch(client, since) -> Batch), SourceHealth
    usgs.py              feeds + fdsnws + detail parsing
    tsunamigov.py        NTWC / PTWC Atom, CAP (namespaced xml.etree), bulletin text
    nws.py               api.weather.gov tsunami alerts
    jma.py               JMA tsunami list + per-event detail, hard-coded Kind.Code table
    ndbc.py              DART activestations.xml + .dart parser + event-mode detection + cubic detrend
    coops.py             CO-OPS stations, water levels, predictions, residual
    geo.py               haversine, bearing, dateline wrap, compass text, nearest-N selection
  state/
    store.py             canonical in-memory store, merge/dedupe, seen state, emits Events on change
    events.py            Event classes (pattern of cfbtui state/events.py)
    rules.py             AlertRules from config: thresholds, home radius, PAGER level, tsunami levels
    history.py           SQLite single-writer task (asyncio.Queue), WAL, retention, day queries, seen_at
  notify/
    inapp.py             AttentionTracker + PulseGate (cooldown gate as in cfbtui ui/pulse.py)
    desktop.py           Notifier protocol: Windows toast (winotify via to_thread), notify-send, osascript, bell
  ui/
    app.tcss  theme.py  glyphs.py  labels.py
    widgets/  quake_list.py  map_panel.py  braille.py  tsunami_panel.py  status_bar.py  ticker.py
              banner.py  gauge_strip.py  too_small.py
    screens/  dashboard.py  detail.py  tsunami.py  history.py  filters.py  help.py
  data/
    coastline_10m.json   Natural Earth 1:10m coastline, little or no simplification (public domain; file name set by the M3 PR)
    dart_stations.json   snapshot of NDBC DART station metadata (fallback when NDBC is down)
scripts/   build_exe.ps1  make_coastline.py  snapshot_stations.py  screenshot_terminal.ps1
tests/     fixtures/{usgs,tsunamigov,nws,jma,ndbc,coops,replay}/  test_*.py  __snapshots__/  uat/
docs/      development.md  smoke-test.md  img/
CLAUDE.md  README.md  CHANGELOG.md  LICENSE (MIT)  pyproject.toml  earthwave.spec  .github/workflows/ci.yml
```

### Data flow

```
PollScheduler  (asyncio loop in one Textual worker: @work(exclusive=True, exit_on_error=False))
  └─ per source, each in its own try/except: Source.fetch(client, since) -> Batch(quakes | alerts | readings,
                                                                                fetched_at, not_modified)
       └─ Store.merge(batch, clock) -> list[Event]
            ├─ AlertRules.evaluate(events) -> Notifications -> Notifier (toast / bell)
            ├─ AttentionTracker.observe(events, clock) -> in-app flashes, badges, banner
            └─ History.enqueue(batch, events)  (single writer task, batched transactions)
  └─ app.post_message(PollDone(result, generation)) -> view recomputed from the Store snapshot, never cached
```

Replay and record live in the transport layer, so every source runs unchanged against recorded traffic. `ReplayTransport` advances `ReplayClock` to each recorded `ts` (scaled by `--speed`), so freshness, attention decay, cooldowns, alert expiry, the age column and history windows all see the recorded time, and the July 2025 Kamchatka products replay as live warnings instead of 15-month-old expired ones.

### Models (normalized)

- `Earthquake`: `id source time lat lon depth_km mag mag_type place status tsunami_flag pager_alert sig felt cdi mmi updated url detail_url also_ids`. Canonical id is the USGS id.
- `TsunamiAlert`: `id dedupe_key source level headline areas issued expires updated geometry(circle | polygon | None) text quake_ref`.
- `GaugeReading`: `station_id kind(dart|coops) ts value_m mode(15m|1m|15s|6m)`; `Station`: `id kind name lat lon`.
- `Freshness`: copy cfbtui's (`LOADING|FRESH|STALE|ERROR`, `age`, `stale_after = 2.5 × interval`, `failures`, `error`), computed from the injected clock.

### Store, merge and events

- Keyed by canonical id. An update is material when magnitude changes ≥ 0.2, depth ≥ 10 km, `status` flips to reviewed, `tsunami_flag` or `pager_alert` changes, or `place` changes.
- Events: `QuakeNew`, `QuakeUpdated(changed: tuple[str, ...])`, `QuakeMajor` (mag ≥ `alerts.major_magnitude`), `QuakeNearHome`, `TsunamiIssued`, `TsunamiChanged` (level up/down, areas), `TsunamiCancelled`, `GaugeAnomaly`, `SourceDegraded`, `SourceRecovered`. Frozen dataclasses with a `kind` string and `__str__` for logs, like cfbtui.
- Startup backfill: `4.5_week` + `all_day` feeds, then `significant_month`; the store is "warm" after that and only diffs raise events (no flash storm on launch).
- Filters: changing `hours`/`min_magnitude` beyond what the feeds cover enqueues one fdsnws query tagged with the scheduler's generation counter; results from an older generation are dropped (cfbtui `switch()` pattern). The visible list is recomputed from the store on every `PollDone`.
- Seen: a row is seen once it has been rendered inside the viewport for one tick or the cursor lands on it; `a` acks everything. `seen_at` persists in SQLite from M5 so acks survive restart.

### In-app live notifications (the extra must-have)

- `AttentionTracker` keeps `id -> Attention(kind, started, acked)` with four kinds and decay rules: `new` (3 cyan flashes over 3 s, then a dim highlight until seen), `updated` (one yellow flash), `major` (orange/red pulsing row and map marker for 60 s, ticker line, in-app `app.notify` toast), `tsunami` (red banner across the dashboard, tsunami panel border pulses, status bar turns red until acknowledged or cancelled). All timing uses the injected clock.
- One `set_interval(0.5)` in the app bumps a `tick` reactive; widgets refresh only the lines with attention. `PulseGate` cooldown (120 s per id+kind) stops re-flashing on repeated minor updates.
- Row coloring needs per-row backgrounds and blink phases, which `DataTable` cannot express via CSS, so `QuakeList` is a custom Line-API widget (`render_line`) like cfbtui's `score_grid.py`. Columns: age, mag (color by band), depth, place, distance/bearing from home, flags (`T` tsunami, PAGER dot). Sort by time, magnitude, distance.
- Color bands: <3 grey, 3–4.4 cyan, 4.5–5.4 green, 5.5–6.4 yellow, 6.5–6.9 orange, ≥7 red bold. PAGER colors as USGS. Tsunami: Warning red, Advisory orange, Watch yellow, Threat/Information blue.

### Screens and keys

- Dashboard: ticker (latest major event), `QuakeList` (left), map panel (right top), tsunami panel (right bottom), status bar (per-source freshness dots and ages, counts for the window, next-poll countdown, filter summary, unseen badge). Below 100 columns the map auto-hides; below 100×30 a "terminal too small" overlay replaces the layout.
- Keys: `j`/`k`/arrows move; `Enter` detail; `f` filters; `t` tsunami screen; `m` toggle map, `M` fullscreen map; `h` history; `o` observations for the selected quake; `s` sort; `/` search place; `g` jump to home; `z` zoom cycle; `a` ack all; `r` refresh now; `?` help; `q` quit. In detail: `b` opens the USGS page with `webbrowser.open`, `y` copies the URL with `App.copy_to_clipboard` (OSC 52).
- Detail screen: all fields, PAGER alert, ShakeMap/DYFI MMI, felt reports, nearby cities, distance from home, linked tsunami alerts, URLs. Detail JSON fetched lazily on open.
- Tsunami screen: alert table (level, center, areas, issued/expires, source), the selected alert's CAP description/instruction or bulletin text, and the gauge strip.
- History screen (M5): day navigation (←/→, local midnight → UTC range), that day's quakes and alerts, a stats line, `Enter` opens detail.
- Help screen includes data attribution (USGS, NOAA/NWS, JMA, Natural Earth).

### Map panel

- `scripts/make_coastline.py` (dev-only, uses `pyshp`) downloads Natural Earth coastline at 1:10m (1:50m is the minimum), applies little or no simplification, and writes `data/coastline_10m.json` as `[[lon, lat], ...]` polylines. There is no file size target. Committed; the script is for regeneration. The M3 PR (#15) regenerates the data to this rule; no M1 code depends on the file name.
- `braille.py`: `BrailleCanvas(cols, rows)` with a 2×4 dot grid per cell, Bresenham line plotting, equirectangular projection for a viewport `(lon0, lat0, lon1, lat1)`. Coastline raster cached per viewport+size; only markers redraw each tick.
- Markers over the raster: `·` `•` `●` `◉` by magnitude band, colored, blinking while they have attention; `⌂` for home; a ring glyph at the source quake of any active tsunami alert, and alert geometry (NWS polygon or CAP circle) tinted along the coast.
- `glyphs = "auto"` picks unicode except on Windows without `WT_SESSION` (conhost), where it picks ascii: a 1×1 cell coastline with `.`/`:` and ASCII markers. `--glyphs` overrides.
- Keys: `m`, `M`, `z` cycle world → region around the selected quake → home, arrows pan when the map has focus.

### Tsunami observations

- Station metadata: `activestations.xml` fetched hourly (DART rows have `type="dart"`), with `data/dart_stations.json` as the bundled fallback (regenerated by `scripts/snapshot_stations.py`); CO-OPS `stations.json` fetched at startup and cached for a day.
- Selection: when any alert is active, or `o` is pressed on a quake with `tsunami_flag`, pick the nearest 6 DART stations within 3000 km of the source quake (CAP `EventLatLon` or the linked USGS quake) and the nearest 4 CO-OPS gauges to the alert geometry. Geometry chain: NWS `geometry` → CAP `circle` → the linked quake. Only selected stations are polled.
- Cadence: `.dart` every `poll.dart_seconds` (900) normally and every `poll.alert_gauge_seconds` (60) while a Warning/Advisory/Watch is active or the newest rows show `T` 2 or 3. CO-OPS `water_level` (6-min, quality flags) every `poll.coops_seconds` (300); `one_minute_water_level` every 60 s while an alert is active.
- Residuals: DART residual = observed − cubic polynomial fitted to the prior 3 h of readings (NOAA's detiding approach; a 3 h median is wrong because the deep-ocean tide moves 0.5–1 m over that span). CO-OPS residual = observed − `predictions` at the same timestamps (1-min predictions to be confirmed at M4; fall back to 6-min).
- Display: `gauge_strip.py` shows one row per station: name, distance from the quake, mode, age of the last reading or last batch ("last batch 3h ago" in standard mode, never implying a live stream), a Textual `Sparkline` of `abs(residual)` padded with `[0, threshold]` so the threshold frames the strip (Sparkline autoscales, so raw ±2 cm would render as full-height noise), and signed max residual. `GaugeAnomaly` fires above `alerts.gauge_anomaly_dart_m` (default 0.05, tune from the Kamchatka fixture) or `alerts.gauge_anomaly_gauge_m` (default 0.3).

### History and replay

- SQLite in `platformdirs.user_data_dir("earthwave")`, WAL mode, one connection (`check_same_thread=False`) owned by a single writer task draining an `asyncio.Queue`, batched transactions per poll. Tables: `quakes` (with `seen_at`), `tsunami_alerts`, `gauge_readings`. Retention `history.retain_days` (default 90) enforced at startup. No `event_log`/`source_runs` tables; the record JSONL covers diagnostics.
- `--record PATH` tees every fetch through `RecordingTransport`. `--replay PATH --speed N` serves it back through `ReplayTransport` with `ReplayClock`. `--offline` loads the store from SQLite and polls nothing. Replay also drives the README screenshots and the end-to-end test.

### Desktop notifications

- `Notifier` protocol in `notify/desktop.py`. Windows: `winotify` (spawns hidden PowerShell per toast, hundreds of ms, can be blocked by policy) called via `asyncio.to_thread` with failures swallowed and logged; `--selftest` never depends on it. Linux: `notify-send`. macOS: `osascript`. Terminal bell via `app.bell()`. All off under `--selftest`, tests and replay unless `--notify` is passed; tests use a recording fake `Notifier`.
- Rules from config: toast on `QuakeMajor`, `QuakeNearHome`, PAGER ≥ `alerts.pager_min`, every `TsunamiIssued`/`TsunamiChanged`. At most one toast per event id; bursts collapse into "N new quakes ≥ M5 in the last minute".

### Logging

`--log PATH` or `--debug` writes a rotating file under `platformdirs.user_log_dir("earthwave")` (default level WARNING, DEBUG with `--debug`); `textual console` stays usable in development. Every source logs status, bytes, duration and 304s at DEBUG.

### Config (`%LOCALAPPDATA%\earthwave\config.toml`, written as a commented template on first run)

```toml
[poll]
usgs_seconds = 60
tsunami_seconds = 60
dart_seconds = 900
coops_seconds = 300
alert_gauge_seconds = 60
[view]
min_magnitude = 2.5
hours = 24
glyphs = "auto"           # auto | unicode | ascii
map = true
time_zone = "local"       # local | UTC
[home]
name = ""                 # empty = no home; distance column hidden
lat = 0.0
lon = 0.0
radius_km = 500
[alerts]
major_magnitude = 6.0
near_home_magnitude = 3.0
pager_min = "yellow"
tsunami = true
gauge_anomaly_dart_m = 0.05
gauge_anomaly_gauge_m = 0.3
[notify]
desktop = true
bell = true
contact = ""              # appended to the User-Agent for the NWS API
[sources]
usgs = true
tsunamigov = true
nws = true
jma = true
[history]
retain_days = 90
```

`tomli-w` drops comments on rewrite, which is fine because the app only writes the template once and otherwise reads.

### Dependencies

Runtime: `textual>=8.2,<9`, `httpx>=0.28,<1`, `platformdirs>=4,<5`, `tomli-w>=1,<2`, `winotify` (win32 only). Dev: `pytest pytest-asyncio respx pytest-xdist pytest-cov pytest-textual-snapshot ruff vulture mypy pyinstaller>=6.22,<7 build pyshp`, with Textual pinned to an exact version in the dev extra so snapshots do not churn on minor releases.

## Execution: one session per milestone

- Each milestone below is one session, one branch and one PR, started fresh with no shared transcript. Sessions may run in parallel in separate git worktrees (each with its own `.venv`) when their files do not overlap (the earlier "no worktrees" rule is relaxed after M1); otherwise they run one at a time in the main checkout at `C:\earthwave`. Only the managing session edits `docs/plan.md` while sessions run in parallel. Branch names: `m0-bootstrap`, `m1-earthquake-core`, `m2-tsunami-alerts`, `m3-world-map`, `m4-observations`, `m5-history`, `m6-release`.
- A session reads `CLAUDE.md` and its milestone section in `docs/plan.md` first, implements only that milestone, and stops. Anything discovered outside its scope goes into a GitHub issue or the "Deferred" list at the end of `docs/plan.md`, never into the session.
- Before opening its PR, a session (1) ticks its milestone's checkbox in `docs/plan.md` and writes a short "State after Mn" note there (what exists, what was deviated from and why, what the next session must know), (2) appends its changelog draft lines to the "Changelog draft" list in `docs/plan.md` (not to `CHANGELOG.md`; see Versioning and changelog), (3) runs the full verification block. The next session starts after the PR is merged.
- Brief template for starting a milestone session (fill in N and the slug):

  > Implement milestone MN of `docs/plan.md` in `C:\earthwave`. Read `CLAUDE.md` and `docs/plan.md` first, including the "State after M(N-1)" note. Work on branch `mN-<slug>`, open a PR against `main`, and consider it done only when the MN acceptance checks and the verification block pass. Do not start other milestones; put anything out of scope in the Deferred list. No Claude attribution anywhere: no `Co-Authored-By` trailer in commits, no "Generated with Claude Code" footer in the PR, no mention of Claude or AI authorship in docs.

- M0's session brief differs only in that the repo has just the plan committed as `docs/plan.md`; M0 creates everything else.

### Versioning and changelog

- ZeroVer: `__version__` lives in `earthwave/__init__.py`, starts at `0.0.1` in M0, becomes `0.1.0` at the M6 release and never reaches 1.0. Minor bumps for new features, patch bumps for fixes. Tags are `v0.y.z` and must match `__version__` and the changelog section, as cfbtui's `release.yml` enforces.
- `CHANGELOG.md` follows Common Changelog (https://common-changelog.org/). Rules that matter here:
  - File starts with `# Changelog`; releases sorted latest-first; a second-level heading per release in the form `## [0.1.0] - 2026-11-15` with a reference-style link to the GitHub release; the version must match the git tag (`v0.1.0`); commit the entry before tagging.
  - No Unreleased section. Milestone PRs are not releases, so sessions do not touch `CHANGELOG.md`; each session appends its draft lines to a "Changelog draft" list in `docs/plan.md`, and the release session (M6, and every later release) composes the entry from that draft plus `git log`.
  - A release holds change groups in this order only: `### Changed`, `### Added`, `### Removed`, `### Fixed`; each group is one bullet list; breaking changes first (prefixed `**Breaking:**`), then by importance, then latest-first.
  - One line per entry, present-tense imperative verb first (Add, Fix, Bump), followed by references in parentheses as Markdown links (commit required, PR or issue when available), then authors in parentheses; authors may be omitted on a single-contributor project.
  - Exclude noise: dotfiles, dev-only dependency changes, style-only changes, doc formatting, revert pairs. Do include refactorings, runtime support changes, and new documentation. Never paste `git log` verbatim.
  - A release with nothing to list uses a single emphasized notice such as `_First release._`.
  - `CHANGELOG.md` in M0 contains only the title; the first entry is written at the `0.1.0` release in M6.

## Data resolution rule

Geographic and generated data (coastlines, borders, plate boundaries, tsunami zones, station lists) must be high resolution, not oversimplified. File size is not a constraint. The linecast project (github.com/ashuttl/linecast) is the minimum detail bar; Natural Earth 1:10m is the target.

## Milestones (one session, one branch, one PR each, in order)

- [x] M0 Bootstrap
- [x] M1 Earthquake core and infrastructure
- [ ] M2 Tsunami alerts and alerting
- [ ] M3 World map
- [ ] M4 Observations
- [ ] M5 History
- [ ] M6 Build and release

### M0: Bootstrap
- `py -3.13 -m venv .venv` in `C:\earthwave` (Python 3.13 already installed; the repo already exists with the plan committed).
- `pyproject.toml` (setuptools, `requires-python >=3.13`, dependencies above, version read from `earthwave.__version__ = "0.0.1"`), ruff/pytest config copied from cfbtui, `CLAUDE.md` (module layout, commands, the one-session-per-milestone rule, ZeroVer, Common Changelog rules, PR flow, and the no-Claude-attribution rule for commits, PRs and docs), `docs/plan.md` (this plan, with the milestone checklist and a "State after M0" note), `README.md` stub, `CHANGELOG.md` in Common Changelog form, MIT `LICENSE`, `.gitignore`, `.gitattributes` (`tests/fixtures/**` as `-text`, `.ps1` CRLF), `ci.yml` (ruff, vulture, pytest, `--version`, `--selftest` on Windows/Linux/macOS).
- `__main__.py` with `--version` and `--selftest` (imports and fixture parsing in M0; the headless frame is added in M1 once an App exists).
- Capture live fixtures now, before they roll off: both tsunami.gov Atom feeds, their CAP files and bulletins (the Oct 25 Bonin Islands statement), `all_hour`, `4.5_week`, `significant_month`, one detail payload with `losspager` and `shakemap`, `activestations.xml`, `46402.dart`, a CO-OPS `water_level` sample, and JMA `list.json` plus its detail.
- Acceptance: `python -m earthwave --version` and `--selftest` pass; CI green. Then ask whether to create `WPTK/earthwave` on GitHub.

### M1: Earthquake core and infrastructure
- `clock.py`, `net.py` (client factory, `RecordingTransport`, `ReplayTransport`, `--record`/`--replay`), logging, `config.py` with first-run template, `models.py`, `sources/usgs.py` (feeds, fdsnws, lazy detail), `sources/geo.py`, `state/store.py`, `state/events.py`, `poll.py` with generation counter.
- Dashboard with `QuakeList`, detail screen (open/copy URL), filters modal (fdsnws backfill on wider filters), status bar, help screen, "too small" overlay, map placeholder, basic attention (new-row flash, unseen badge), `--selftest` headless frame.
- Fixtures: saved feeds, one detail payload, one 304 case, one short recorded session. Tests: parsing, merge and material-update rules, scheduler backoff and generation with an injected clock, replay transport round-trip, flashing asserted through `QuakeList.render_line(y)` styles at injected clock steps, Pilot tests for open-detail and filter flows, a dashboard snapshot at 120×40 with fixture quakes.

### M2: Tsunami alerts and alerting
- `sources/tsunamigov.py` (Atom poll, follow each new entry's CAP link, namespaced `xml.etree` parsing, bulletin text), `sources/nws.py` (three event types, User-Agent, 429 handling), `sources/jma.py` (list + detail, `en_*` fields, hard-coded code table), USGS `tsunami_flag` linkage, `TsunamiAlert` merge keyed on WMO header + `sent` with quake+areas fallback, cancellations closing by `references`, expiry on the injected clock.
- Tsunami panel, tsunami screen (alert table, description/instruction or bulletin text), red banner, status bar color states, `state/rules.py`, `notify/desktop.py` with the fake `Notifier` for tests, `notify/inapp.py` with `PulseGate`.
- Fixtures: the captured Bonin Islands information statement; archived Warning/Advisory/Watch/Cancel products from tsunami.gov per-event URLs (the July 2025 Kamchatka M8.8 Pacific-wide warnings are a good set); one NWS tsunami alert GeoJSON; a JMA list + detail pair. A replay fixture that walks Watch → Warning → Cancellation drives the UI test and a tsunami-screen snapshot, since live feeds are quiet most days.

### M3: World map
- `scripts/make_coastline.py`, `data/coastline_10m.json`, `ui/widgets/braille.py`, `map_panel.py`, zoom/pan, auto/ascii glyph selection, attention blinking on markers, tsunami geometry highlighting.
- Tests: projection math, canvas plotting of known segments, snapshots of the world view at 80×24 (ascii) and 160×40 (unicode).

### M4: Observations
- `sources/ndbc.py` (`activestations.xml`, `.dart` parser, event-mode detection, cubic detrend), `sources/coops.py` (stations, `water_level`, `one_minute_water_level`, `predictions`, confirm 1-min predictions), `scripts/snapshot_stations.py` → `data/dart_stations.json`, station selection and geometry chain in `sources/geo.py`, `gauge_strip.py` on the tsunami screen, `GaugeAnomaly` events, alert-driven cadence switch.
- Fixtures: the live `46402.dart` (standard mode) plus an event-mode file with `T` 2 and 3 rows, `activestations.xml`, a CO-OPS `water_level` + `predictions` pair and a `one_minute_water_level` sample. Tests: detrend residual on a synthetic tide plus an injected 10 cm pulse, sparkline framing, cadence switch under replay.

### M5: History
- `state/history.py` (single writer task, schema, retention, `seen_at`), `--offline`, history screen with local-day windows, end-to-end replay test that quits and relaunches offline.

### M6: Build and release
- `earthwave.spec` + `scripts/build_exe.ps1` bundling `data/` (no `tzdata`), `dist\earthwave.exe --selftest`, README with replay-driven screenshots (`scripts/screenshot_terminal.ps1` as in cfbtui), `docs/development.md`, `docs/smoke-test.md`, CHANGELOG, tag `v0.1.0`.
- Post-v1 candidates, not scheduled: EMSC websocket, IOC gauges, GeoNet/JMA quake lists, a Settings screen, self-updater, and an `all_day` performance pass only if profiling shows a need (the feed is a few hundred features, and `json.loads` in a thread does not release the GIL).

## Verification

```powershell
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m vulture earthwave scripts --min-confidence 80
.venv\Scripts\python -m pytest -n auto
.venv\Scripts\python -m pytest --snapshot-update          # only after an intentional UI change
.venv\Scripts\python -m earthwave --selftest
.venv\Scripts\python -m earthwave --debug                  # live, in Windows Terminal
.venv\Scripts\python -m earthwave --replay tests\fixtures\replay\kamchatka_watch_to_warning.jsonl --speed 20 --notify
scripts\build_exe.ps1; dist\earthwave.exe --selftest
```

Manual checks per milestone:
- M1: the quake list matches earthquake.usgs.gov for the same window; the debug log shows 304s between changes; killing the network turns the USGS dot to ERROR then STALE and recovers without restart; widening filters to 7 days / M1.0 fills the list from fdsnws; a crash in one source leaves the app running.
- M2: replay of the Kamchatka fixture shows banner, panel pulse, red status bar, a Windows toast and a bell with the alert shown as live (not expired); cancellation clears them. The real Bonin Islands information statement renders blue with no toast.
- M3: coastlines recognizable at 80×24; a known quake (e.g. the Oct 25 Bonin Islands M7.0) lands on the right spot; ascii mode readable in conhost.
- M4: during replay with the event-mode DART fixture, the strip shows the residual pulse and the anomaly flag; in standard mode it reads "last batch Nh ago".
- M5: quit, relaunch with `--offline`, yesterday's events and acks are still there; retention trims rows older than 90 days.
- M6: the exe runs on a PC without Python (`docs/smoke-test.md`).

## State after M0

- The public repo `WPTK/earthwave` now exists; `m0-bootstrap` is pushed with a PR against `main`. `ci.yml` is written and first runs on that PR.
- Exists: `pyproject.toml` (ruff/pytest config mirrored from cfbtui; `pytest-textual-snapshot` and `pyshp` unpinned in the dev extra, Textual pinned only by the `<9` range, so pin it exactly when the first snapshot lands in M1), `CLAUDE.md`, README stub, title-only `CHANGELOG.md`, MIT `LICENSE`, `.gitignore`, `.gitattributes`, `earthwave/__init__.py` (`0.0.1`), `__main__.py` (`--version`, `--selftest`), `scripts/capture_fixtures.py`, `tests/test_basic.py`.
- Fixtures under `tests/fixtures/` captured 2026-10-31, all sources: `tsunamigov/` (PAAQ and PHEB Atom, each with its one CAP and bulletin), `usgs/` (`all_hour`, `4.5_week`, `significant_month`, `detail` with losspager and shakemap), `ndbc/` (`activestations.xml`, `46402.dart`), `coops/water_level_9414290.json`, `jma/` (`list.json`, `detail.json`).
- Deviations: repo root is `C:\earthwave` (built on Gilead, not the gaming PC); no winget install step; the plan was edited in place rather than re-committed. `--selftest` skips fixture parsing when `tests/fixtures` is absent (a packaged exe), so M6 can bundle or drop that.
- Next session (M1): the repo has no `earthwave/` modules beyond the CLI; `.venv` is untracked and must be created with `py -3.13 -m venv .venv` then `pip install -e ".[dev]"`.

## State after M1

- Exists: everything in the M1 section. `clock.py`, `net.py`, `logging_setup.py`, `config.py`, `models.py`, `poll.py`, `app.py`, `sources/{base,usgs,geo}.py`, `state/{store,events}.py`, `notify/inapp.py` (AttentionTracker only), `ui/{theme,glyphs,labels,app.tcss}`, `ui/widgets/{quake_list,status_bar,ticker,map_panel,too_small}.py`, `ui/screens/{dashboard,detail,filters,help}.py`. Package `__init__.py` files are empty. `--selftest` also renders one headless 120x40 frame. Tests: 90, with a dashboard snapshot; Textual is pinned to `8.2.8` in the dev extra. M3 (`braille.py`, coastline) and M4 (`ndbc.py`, `coops.py`, station snapshot) are pure modules and may land in parallel with or before M2 without touching M1 files.
- Verified live on 2026-10-31: the startup chain (`4.5_week`, `all_day`, `significant_month`) then `2.5_day` returns 200 once and 304 (`If-Modified-Since` from the `Last-Modified` cursor) on the next poll.
- Public shape of `earthwave/models.py` (all frozen, slotted dataclasses, aware-UTC datetimes):
  - `Earthquake(id, time, lat, lon, mag, depth_km=0.0, source="usgs", mag_type="", place="", status="automatic", tsunami_flag=False, pager_alert=None, sig=0, felt=None, cdi=None, mmi=None, updated=None, url="", detail_url="", also_ids=())`
  - `QuakeDetail(id, pager_alert, pager_max_mmi, shakemap_max_mmi, shakemap_max_pga, shakemap_max_pgv, dyfi_max_mmi, dyfi_responses, nearby_cities, products)` (all but `id` optional)
  - `TsunamiAlert(id, dedupe_key, source, level, headline, areas=(), issued=None, expires=None, updated=None, geometry=None, text="", quake_ref=None)`; `geometry` is `("circle", lat, lon, radius_km)`, `("polygon", ((lon, lat), ...))` or `None`
  - `Station(id, kind, name, lat, lon)`; `GaugeReading(station_id, kind, ts, value_m, mode)`
  - `Batch(source, fetched_at, quakes=(), alerts=(), readings=(), stations=(), not_modified=False, backfill=False, cursor=None, coverage=None, meta={})`: `cursor` is opaque and handed back to that source's next `fetch` as `since`; `backfill` batches merge silently; `coverage` is `(hours, min_magnitude)`.
- Public shape of `earthwave/sources/base.py`: `SourceHealth(failures=0, last_ok=None, last_error=None, polls=0)` and the `Source` protocol: attributes `name: str` and `min_interval: float` (the scheduler never polls faster), `interval(health) -> float` (seconds after a success; 0 = again immediately), `async fetch(client: httpx.AsyncClient, since: str | None) -> Batch` (raise on any failure; the scheduler catches it, applies backoff and emits `SourceDegraded`/`SourceRecovered`).
- Adding a source (M2-M4): implement the protocol, add it to the `sources` list in `EarthwaveApp.__init__` (gated by `config.sources`), and extend `Store.merge` (alerts and readings are not merged yet; `Batch.alerts/readings/stations` are carried but ignored). `PollResult(source, generation, events, batch, error)` goes to the app as the `PollDone` message; `AttentionTracker.observe` ignores event types it does not know, so M2 adds the `tsunami` kind there and `PulseGate`.
- Scheduler: `Scheduler(sources, store, clock, on_done, rng=None)`; `poll_source(name, client)` never raises; `submit_backfill(source, client, job)` runs a one-off query whose result is dropped if `switch()` bumped `generation` meanwhile; `refresh_now()` honors `min_interval`; backoff is interval x 2 per failure, capped at 600 s, +-10% jitter, 30 s base for a source whose interval is 0.
- USGS source: startup chain once (silent backfill), then the smallest day feed that contains the view's `min_magnitude` (`steady_feed`); `set_min_magnitude` switches it, and `EarthwaveApp.apply_filters` backfills from fdsnws whenever the steady feed changed or `Store.is_covered(hours, min_magnitude)` is false. The store is "cold" until the first steady batch is merged; cold and backfill merges raise no events. Non-earthquake events (blasts) and features without a magnitude are skipped.
- Replay: `ReplayTransport` serves the n-th request for a URL from the n-th record (the last repeats; unknown URL gives 404) and moves `ReplayClock` forward to the record's `ts`. `tests/fixtures/replay/usgs_short.jsonl` is composed from trimmed captured payloads (startup chain, `2.5_day` 200, 304, then a 200 with one revised and one synthetic M6.2 quake `replay_new1`); the new quake and its timestamps are synthetic.
- Deviations: the map hides below 120 columns (not 100) because the overlay already replaces the layout below 100x30; the right column is only the map placeholder (the tsunami panel arrives in M2); `--notify` and `--offline` are not CLI flags yet (M2 and M5 add them); keys `t h o g z M` arrive with their milestones; the filter modal takes minimum magnitude and hours only; `pyproject.toml` ships `ui/*.tcss` and `data/*.json` as package data.
- Next sessions: M2 builds on `models.py`, `Store`, `AttentionTracker` and the `Scheduler`.

## Risks and mitigations

- Tsunami feeds are quiet most of the time: capture the current information statement and archived warning products in M0/M2 and build everything from fixtures and replay with the replay clock.
- NWS API requires a User-Agent and only covers US areas: global coverage comes from PTWC through tsunami.gov, with JMA for the NW Pacific.
- tsunami.gov CAP is signed XML with namespaces and no directory listing: parse by namespace, discover products only through the Atom entries, tolerate a missing `polygon`.
- DART standard-mode data arrives in 6-hour batches: the UI says "last batch" rather than implying a live stream; event mode is where 1-min data appears.
- JMA's endpoint is undocumented and Japanese-first: tolerant parsing, `en_*` fields for display, hard-coded code table, attribution in help.
- `winotify` shells out to PowerShell: run it off the event loop, swallow failures, never block startup or self-test on it.
- Braille on conhost: `glyphs = auto` falls back to ascii when `WT_SESSION` is unset on Windows.
- Rate limits are undocumented on USGS/NOAA: fixed 60 s floor, conditional requests, backoff, one client.

## Open items to confirm during execution

- Create `WPTK/earthwave` on GitHub (not chosen in scoping; ask first). Created as a public repo after M0 was built.
- Default home location: none until set in `config.toml` or `--home`.

## Changelog draft

Each milestone session appends its notable changes here as Common Changelog entry lines (imperative verb first, with PR reference), grouped under Changed / Added / Removed / Fixed. The release session turns this into the `CHANGELOG.md` entry and clears it.

### Added
- Add `--version` and `--selftest` commands and project scaffolding.
- Add a live earthquake dashboard from USGS feeds with a quake list, detail screen, filters, search, sorting and a status bar showing per-source freshness.
- Add flashing rows for new, updated and major quakes, an unseen badge and an in-app toast for major quakes.
- Add `fdsnws` history backfill when filters reach beyond what the feeds cover.
- Add `--record` and `--replay` to capture and replay HTTP sessions with a replay clock, plus `--config`, `--no-config`, `--min-mag`, `--hours`, `--home`, `--glyphs`, `--log` and `--debug`.
- Add a commented first-run config file under the user config directory.
- Add a headless frame to `--selftest`.

## Deferred

Items found during a milestone session that belong to a later one or to post-v1 go here, one line each, with the session that found them.

- M1: the steady feed is a day feed, so revisions to quakes older than 24 h (for example a later `reviewed` flip) are only picked up when a filter change triggers an fdsnws query; an `updatedafter` fdsnws poll would close this.
- M1: `Store.merge` keys on the USGS id only; when USGS re-associates events and changes the canonical id, the old row lingers until it ages out (`also_ids` is stored but not used to merge).
- M1: nearby-cities parsing for the detail screen is tested only against a synthetic payload; the captured detail fixture has no `nearby-cities` product.
- M1: `PulseGate` (repeat-flash cooldown) is not needed until updated rows can fire repeatedly; M2 adds it with the `tsunami` attention kind.
- M1: the 60 s floor means pressing `r` soon after a poll does nothing visible; consider showing the wait in the toast.
