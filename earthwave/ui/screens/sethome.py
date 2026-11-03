"""Set-home modal: the in-app way to write the [home] table (the same check and writer as --home / --save-home)."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label

from earthwave.config import ConfigError, HomeConfig, parse_home

NAME_MAX = 60


def parse_home_fields(location: str, name: str, current: HomeConfig) -> tuple[float, float, str]:
    """Validate the two fields; raises ValueError with a message that names the field. An empty name keeps the
    configured one (else "home"), as `--save-home` does."""
    try:
        lat, lon = parse_home(location, "Location")
    except ConfigError as exc:
        raise ValueError(str(exc)) from None
    name = name.strip()
    if len(name) > NAME_MAX or any(not c.isprintable() for c in name):
        raise ValueError(f"Name must be printable text of at most {NAME_MAX} characters")
    return lat, lon, name or current.name or "home"


class SetHomeScreen(ModalScreen[tuple[float, float, str] | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, current: HomeConfig):
        super().__init__()
        self._current = current
        self._showing_error = False

    def compose(self) -> ComposeResult:
        here = self._current
        with Vertical(id="dialog"):
            yield Label("Set home", id="dialog_title")
            yield Label("Location: LAT,LON")
            yield Input(f"{here.lat:g},{here.lon:g}" if here.enabled else "", id="location", placeholder="40.71,-74.01")
            yield Label("Name (optional)")
            yield Input(here.name, id="home_name", placeholder="home")
            yield Label("", id="error")
            with Horizontal(id="buttons"):
                yield Button("Save", id="apply", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#location", Input).focus()

    def _check(self) -> tuple[float, float, str] | None:
        try:
            result = parse_home_fields(
                self.query_one("#location", Input).value, self.query_one("#home_name", Input).value, self._current
            )
        except ValueError as exc:
            self.query_one("#error", Label).update(str(exc))
            self._showing_error = True
            return None
        self.query_one("#error", Label).update("")
        self._showing_error = False
        return result

    def _submit(self) -> None:
        if (result := self._check()) is not None:
            self.dismiss(result)

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        if self._showing_error:
            self._check()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "apply":
            self._submit()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
