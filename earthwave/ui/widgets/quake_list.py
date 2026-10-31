"""The quake list: a Line-API widget so every row can carry its own flash color and blink phase."""

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.binding import Binding
from textual.events import Click
from textual.geometry import Region, Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.strip import Strip

from earthwave.clock import Clock
from earthwave.config import HomeConfig
from earthwave.models import Earthquake
from earthwave.notify.inapp import AttentionTracker
from earthwave.ui.glyphs import Glyphs
from earthwave.ui.labels import fmt_age, fmt_distance
from earthwave.ui.theme import PAGER_COLORS, mag_color

AGE_W, MAG_W, DEPTH_W, DIST_W, FLAG_W = 4, 4, 5, 11, 2
CURSOR_BG = "#1e3a5f"


def place_width(width: int, has_home: bool) -> int:
    fixed = 1 + AGE_W + 1 + MAG_W + 1 + DEPTH_W + 1 + (DIST_W + 1 if has_home else 0) + 1 + FLAG_W
    return max(0, width - fixed)


def header_text(width: int, has_home: bool) -> Text:
    parts = [" ", "AGE".rjust(AGE_W), " ", "MAG".rjust(MAG_W), " ", "DEPTH".rjust(DEPTH_W), " "]
    if has_home:
        parts += ["FROM HOME".rjust(DIST_W), " "]
    parts += ["PLACE".ljust(place_width(width, has_home)), " ", "FL"]
    return Text("".join(parts)[:width].ljust(width), style="bold grey62 underline")


class QuakeList(ScrollView, can_focus=True):
    BINDINGS = [
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("pagedown", "page_down", "Page down", show=False),
        Binding("pageup", "page_up", "Page up", show=False),
        Binding("home", "cursor_first", "First", show=False),
        Binding("end", "cursor_last", "Last", show=False),
        Binding("enter", "select", "Detail", show=False),
    ]

    class Selected(Message):
        def __init__(self, quake: Earthquake):
            super().__init__()
            self.quake = quake

    class Highlighted(Message):
        def __init__(self, quake: Earthquake):
            super().__init__()
            self.quake = quake

    def __init__(
        self,
        clock: Clock,
        tracker: AttentionTracker,
        glyphs: Glyphs,
        home: HomeConfig | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._clock = clock
        self._tracker = tracker
        self._glyphs = glyphs
        self._home = home if home and home.enabled else None
        self.rows: list[Earthquake] = []
        self.cursor = 0

    # --- data ---------------------------------------------------------------------------------------------------

    def set_rows(self, rows: list[Earthquake]) -> None:
        """Replace the rows. The cursor stays on its quake and, if it was on screen, on the same screen line (kept
        visible after a reorder); a list scrolled to the top stays at the top. A cursor scrolled out of sight leaves
        the view anchored on its top row."""
        selected, height, scroll = self.selected, max(self.size.height, 1), self.scroll_offset.y
        line = self.cursor - scroll if selected is not None and 0 <= self.cursor - scroll < height else None
        top = self.rows[scroll].id if 0 < scroll < len(self.rows) else None
        self.rows = rows
        found = selected is not None and any(q.id == selected.id for q in rows)
        if found:
            self.cursor = next(i for i, q in enumerate(rows) if q.id == selected.id)
        else:
            self.cursor = 0 if selected is None else min(self.cursor, max(len(rows) - 1, 0))
        self.virtual_size = Size(max(self.size.width, 1), len(rows))
        target = None
        if found and line is not None:
            target = 0 if scroll == 0 and self.cursor < height else max(self.cursor - line, 0)
        elif top is not None:
            target = next((i for i, q in enumerate(rows) if q.id == top), None)
        if target is not None:
            self.scroll_to(y=target, animate=False, immediate=True)
        self.refresh()

    @property
    def selected(self) -> Earthquake | None:
        return self.rows[self.cursor] if 0 <= self.cursor < len(self.rows) else None

    def visible_ids(self) -> set[str]:
        top = self.scroll_offset.y
        return {q.id for q in self.rows[top : top + self.size.height]}

    def on_resize(self) -> None:
        self.virtual_size = Size(max(self.size.width, 1), len(self.rows))

    # --- rendering ----------------------------------------------------------------------------------------------

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        width = self.scrollable_content_region.width  # size.width includes the scrollbar column
        index = scroll_y + y
        if index >= len(self.rows):
            return Strip.blank(width, Style())
        return self._row_strip(self.rows[index], index == self.cursor, width).crop_extend(
            scroll_x, scroll_x + width, Style()
        )

    def _row_strip(self, quake: Earthquake, selected: bool, width: int) -> Strip:
        attention = self._tracker.style_for(quake.id)
        if attention is not None:
            flat: Style | None = Style(color=attention.fg, bgcolor=attention.bg, bold=attention.bold)
            base = flat
        else:
            flat = None
            base = Style(bgcolor=CURSOR_BG) if selected else Style()

        def field(text: str, color: str = "") -> Segment:
            if flat is not None:
                return Segment(text, flat)
            return Segment(text, base + Style.parse(color) if color else base)

        marker = ("▌" if self._glyphs.unicode else ">") if selected else " "
        age = fmt_age((self._clock.now() - quake.time).total_seconds()).rjust(AGE_W)
        place_w = place_width(width, self._home is not None)
        place = quake.place or "unknown"
        if len(place) > place_w:
            place = place[: max(place_w - 1, 0)] + self._glyphs.ellipsis
        pager = quake.pager_alert[0].upper() if quake.pager_alert in PAGER_COLORS else " "  # G/Y/O/R: not colour alone
        segments = [
            field(marker, "cyan"),
            field(age, "grey62"),
            field(" "),
            field(f"{quake.mag:{MAG_W}.1f}", mag_color(quake.mag)),
            field(" "),
            field(f"{quake.depth_km:.0f}".rjust(DEPTH_W), "grey62"),
            field(" "),
        ]
        if self._home is not None:
            segments += [field(fmt_distance(quake, self._home.lat, self._home.lon).rjust(DIST_W), "grey62"), field(" ")]
        segments += [
            field(place.ljust(place_w)),
            field(" "),
            field(self._glyphs.tsunami if quake.tsunami_flag else " ", "bold blue"),
            field(pager, PAGER_COLORS.get(quake.pager_alert or "", "")),
        ]
        return Strip(segments)

    # --- cursor -------------------------------------------------------------------------------------------------

    def _move(self, index: int) -> None:
        if not self.rows:
            return
        self.cursor = max(0, min(index, len(self.rows) - 1))
        quake = self.rows[self.cursor]
        self._tracker.ack(quake.id)
        self.scroll_to_region(Region(0, self.cursor, max(self.size.width, 1), 1), animate=False)
        self.post_message(self.Highlighted(quake))
        self.refresh()

    def action_cursor_down(self) -> None:
        self._move(self.cursor + 1)

    def action_cursor_up(self) -> None:
        self._move(self.cursor - 1)

    def action_page_down(self) -> None:
        self._move(self.cursor + max(self.size.height - 1, 1))

    def action_page_up(self) -> None:
        self._move(self.cursor - max(self.size.height - 1, 1))

    def action_cursor_first(self) -> None:
        self._move(0)

    def action_cursor_last(self) -> None:
        self._move(len(self.rows) - 1)

    def action_select(self) -> None:
        if (quake := self.selected) is not None:
            self.post_message(self.Selected(quake))

    def on_click(self, event: Click) -> None:
        if (index := self.scroll_offset.y + event.y) < len(self.rows):  # the empty area below the rows is not a row
            self._move(index)
