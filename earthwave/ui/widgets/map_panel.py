"""Placeholder for the terminal world map (milestone M3)."""

from textual.widgets import Static


class MapPanel(Static):
    def __init__(self, **kwargs):
        super().__init__("World map\n\n(coming in a later release)", **kwargs)
