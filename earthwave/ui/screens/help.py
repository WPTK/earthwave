"""Help screen: keys and data attribution."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from earthwave.ui.labels import fmt_age
from earthwave.ui.widgets.status_bar import feed_word

HELP_TEXT = """\
[b]Keys[/b]
  j/k, arrows  move          Enter  quake detail
  f  filters                 s      cycle sort
  /  search place            m      show or hide map
  M  full-screen map         z      zoom map
  t  tsunami alerts          a      acknowledge alerts, new rows
  r  refresh now             ?      this help
  o  tide gauges near quake  Tab    focus map; arrows pan
  h  history by day          q      quit
  g  group aftershocks       Space  open or close a group
  Detail: b open page, y copy URL, Esc back

[b]Colors[/b]
  mag: grey <3, cyan <4.5, green <5.5, yellow <6.5, orange <7, red 7+
  new rows flash cyan, updated rows amber, major quakes pulse red
  tsunami: Warning red, Advisory orange, Watch yellow, others blue
  FL column: T = USGS tsunami flag; G/Y/O/R = PAGER impact, green to red
  map: bigger dot, bigger quake (· • ● ◉, or + o O @ in ASCII)

[b]Data[/b]
  Quakes: USGS (earthquake.usgs.gov), public domain.
  Tsunami and sea level: NOAA / NWS, Japan Meteorological Agency.
  Coastlines: Natural Earth, public domain.
  Not an official tsunami or quake warning; follow local authorities.

[b]Status bar[/b]
  N shown of M   rows passing the filter and search, of all held
  M4.5+ 72h      the filter: magnitude 4.5 and up, last 72 hours (f)
  sort:time      the order of the list (s cycles it)
  next 4:30      time until the sources are polled again
  3 unseen       rows that arrived since you looked (a clears)
  TSUNAMI        an alert is in force (t opens it)
  REPLAY         the data is a recording, not live

[b]Quake list[/b]
  DEPTH          km below the surface; negative is above the datum
  FROM HOME      distance and direction of the quake from home
  Gauges (o)     Residual is sea level minus the predicted tide;
                 Mode is how often the gauge reports (15m, 6m, 1m, 15s)

"""

FEED_NAMES = {
    "usgs": "USGS earthquakes",
    "tsunamigov": "tsunami.gov alerts",
    "nws": "NWS alerts",
    "jma": "JMA tsunami",
    "dart": "DART buoys",
    "coops": "CO-OPS tide gauges",
}


def feeds_text(app) -> str:
    """One line per source: its state and when it last answered, so "feeds ok" can be checked source by source."""
    if app.offline:
        return "[b]Feeds[/b]\n  offline: nothing is fetched, the list is saved history\n\n"
    lines = ["[b]Feeds[/b]"]
    for name in app.scheduler.statuses:
        fresh = app.scheduler.freshness(name)
        age = app.scheduler.age(name)
        state = feed_word(fresh, app.scheduler.statuses[name].health.incomplete)
        last = f", last good {fmt_age(age)} ago" if age is not None else ", no good answer yet"
        lines.append(f"  {FEED_NAMES.get(name, name):<20} {state}{last}")
    return "\n".join(lines) + "\n\n"


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,question_mark", "close", "Close")]

    def compose(self) -> ComposeResult:
        box = VerticalScroll(id="help_box")
        box.border_subtitle = "Esc or ? to close, arrows scroll"
        with box:
            yield Static(HELP_TEXT + feeds_text(self.app), id="help_text")

    def action_close(self) -> None:
        self.dismiss(None)
