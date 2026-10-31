"""Help screen: keys and data attribution."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

HELP_TEXT = """\
[b]earthwave[/b]  earthquakes worldwide

[b]Keys[/b]
  j / k, arrows   move          Enter   quake detail
  f   filters                   s       cycle sort (time, magnitude, distance)
  /   search place              m       show or hide the map
  M   full-screen map           z       zoom map (world, region, home)
  a   acknowledge all           r       refresh now
  Map focused (Tab): arrows pan
  ?   this help                 q       quit
  In detail:  b open USGS page, y copy URL, Esc back

[b]Colors[/b]
  magnitude  grey <3, cyan 3-4.4, green 4.5-5.4, yellow 5.5-6.4, orange 6.5-6.9, red 7+
  new rows flash cyan; updated rows flash yellow; major quakes pulse red for a minute

[b]Data[/b]
  Earthquakes: U.S. Geological Survey (earthquake.usgs.gov), public domain.
  Tsunami alerts and sea-level data: NOAA / National Weather Service, Japan Meteorological Agency.
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
