"""Filter modal: minimum magnitude and time window."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label

from earthwave.state.store import ViewFilter


def parse_filters(mag_text: str, hours_text: str) -> tuple[float, float]:
    """Validate the two fields; raises ValueError with a message fit for the dialog."""
    try:
        mag = float(mag_text)
    except ValueError:
        raise ValueError("Minimum magnitude must be a number") from None
    try:
        hours = float(hours_text)
    except ValueError:
        raise ValueError("Hours must be a number") from None
    if not -2 <= mag <= 10:
        raise ValueError("Minimum magnitude must be between -2 and 10")
    if not 1 <= hours <= 24 * 365:
        raise ValueError("Hours must be between 1 and 8760")
    return mag, hours


class FiltersScreen(ModalScreen[tuple[float, float] | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, view: ViewFilter):
        super().__init__()
        self._view = view

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Filters", id="dialog_title")
            yield Label("Minimum magnitude")
            yield Input(f"{self._view.min_magnitude:g}", id="min_mag", select_on_focus=True)
            yield Label("Hours back (up to 8760)")
            yield Input(f"{self._view.hours:g}", id="hours", select_on_focus=True)
            yield Label("", id="error")
            with Horizontal(id="buttons"):
                yield Button("Apply", id="apply", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#min_mag", Input).focus()

    def _submit(self) -> None:
        try:
            result = parse_filters(self.query_one("#min_mag", Input).value, self.query_one("#hours", Input).value)
        except ValueError as exc:
            self.query_one("#error", Label).update(str(exc))
            return
        self.dismiss(result)

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
