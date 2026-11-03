"""The quake list: a Line-API widget so every row can carry its own flash color and blink phase."""

from rich.cells import cell_len, set_cell_size
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.binding import Binding
from textual.color import Color
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
from earthwave.ui.labels import fmt_age, fmt_depth, fmt_distance
from earthwave.ui.theme import CURSOR_BG, CYAN, PAGER_COLORS, lift, mag_color

AGE_W, MAG_W, DEPTH_W, DIST_W, FLAG_W = 4, 4, 5, 12, 2  # DIST_W fits "20015 km NNE", the longest distance on Earth


def place_width(width: int, has_home: bool) -> int:
    fixed = 1 + AGE_W + 1 + MAG_W + 1 + DEPTH_W + 1 + (DIST_W + 3 if has_home else 0) + 1 + FLAG_W
    return max(0, width - fixed)


def header_text(width: int, has_home: bool) -> Text:
    parts = [" ", "AGE".rjust(AGE_W), " ", "MAG".rjust(MAG_W), " ", "DEPTH".rjust(DEPTH_W), " "]
    if has_home:
        parts += [
            "FROM HOME".rjust(DIST_W),
            "   ",
        ]  # the rows put a rule here, so the distance and the place do not read as one phrase
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
            return Strip.blank(width, self.rich_style)
        return self._row_strip(self.rows[index], index == self.cursor, width).crop_extend(
            scroll_x, scroll_x + width, self.rich_style
        )

    def _row_strip(self, quake: Earthquake, selected: bool, width: int) -> Strip:
        attention = self._tracker.style_for(quake.id)
        # Every cell names both colours: left unset, the terminal's own (a light profile's white) shows through
        ground = self.rich_style
        if attention is not None:
            flat: Style | None = Style(color=attention.fg, bgcolor=attention.bg, bold=attention.bold)
            base = flat
        else:
            flat = None
            base = Style(color=ground.color, bgcolor=CURSOR_BG if selected else ground.bgcolor)
        ground_bg = Color.parse(CURSOR_BG) if selected else Color.from_rich_color(ground.bgcolor)

        def field(text: str, color: str = "") -> Segment:
            if flat is not None:
                return Segment(text, flat)
            if not color:
                return Segment(text, base)
            style = Style.parse(color)
            if (
                selected and style.color is not None
            ):  # a status colour tuned for the ground (ANSI red at 2.8:1 on the navy bar) is tinted to read
                fg = style.color.get_truecolor(self.app.ansi_theme)
                style += Style(color=lift(Color(fg.red, fg.green, fg.blue), ground_bg).rich_color)
            return Segment(text, base + style)

        marker = ("▌" if self._glyphs.unicode else ">") if selected else " "
        age = fmt_age((self._clock.now() - quake.time).total_seconds()).rjust(AGE_W)
        place_w = place_width(width, self._home is not None)
        place = "".join(c if c.isprintable() else " " for c in quake.place).strip() or "unknown"  # no tab/newline
        if cell_len(place) > place_w:
            place = set_cell_size(place, max(place_w - 1, 0)) + self._glyphs.ellipsis
        place = set_cell_size(place, place_w)  # pad in terminal cells, so a wide glyph cannot push the flags out
        mag = max(-9.9, min(quake.mag, 99.9))  # the MAG column is four cells wide
        pager = quake.pager_alert[0].upper() if quake.pager_alert in PAGER_COLORS else " "  # G/Y/O/R: not colour alone
        segments = [
            field(marker, CYAN),
            field(age, "grey62"),
            field(" "),
            field(f"{mag:{MAG_W}.1f}", mag_color(quake.mag)),
            field(" "),
            field(fmt_depth(quake.depth_km).rjust(DEPTH_W), "grey62"),
            field(" "),
        ]
        if self._home is not None:
            segments += [
                field(fmt_distance(quake, self._home.lat, self._home.lon).rjust(DIST_W), "grey62"),
                field(" " + self._glyphs.bar + " ", "grey50"),
            ]
        segments += [
            field(place),
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

    def go_top(self) -> None:
        """Cursor and view to the first row, without marking its quake seen (a new sort starts at its head, #247)."""
        if not self.rows:
            return
        self.cursor = 0
        self.scroll_to(y=0, animate=False, immediate=True)
        self.post_message(self.Highlighted(self.rows[0]))
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
            if event.chain >= 2:  # a double-click opens the detail, as Enter does (#229)
                self.action_select()
