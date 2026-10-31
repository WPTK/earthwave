"""Help screen: keys and data attribution."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

HELP_TEXT = """\
[b]earthwave[/b]  earthquakes worldwide

[b]Keys[/b]
  j/k, arrows  move          Enter  quake detail
  f  filters                 s      cycle sort
  /  search place            m      show or hide map
  M  full-screen map         z      zoom map
  t  tsunami alerts          a      acknowledge alerts, new rows
  r  refresh now             ?      this help
  o  tide gauges near quake  Tab    focus map; arrows pan
  q  quit
  Detail: b open page, y copy URL, Esc back

[b]Colors[/b]
  mag: grey <3, cyan <4.5, green <5.5, yellow <6.5, orange <7, red 7+
  new rows flash cyan, updated rows yellow, major quakes pulse red
  tsunami: Warning red, Advisory orange, Watch yellow, others blue

[b]Data[/b]
  Quakes: USGS (earthquake.usgs.gov), public domain.
  Tsunami and sea level: NOAA / NWS, Japan Meteorological Agency.
  Coastlines: Natural Earth, public domain.

[dim]Esc or ? to close[/dim]
"""


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,question_mark", "close", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="help_box"):
            yield Static(HELP_TEXT)

    def action_close(self) -> None:
        self.dismiss(None)
