"""Place-name labels for the map: a few continents, oceans and seas, drawn in empty cells of the braille raster.

Labels are chosen from the view alone, by fixed priority (list order), and one that does not fit is dropped, never
moved, so a pan changes only where a label is drawn, not which labels win.
"""

from __future__ import annotations

from collections.abc import Collection

from earthwave.ui.widgets.braille import BrailleCanvas, Viewport, project_cell

# (text, lon, lat, kind). Order is priority: earlier labels are placed first and win a collision.
LABELS: tuple[tuple[str, float, float, str], ...] = (
    ("NORTH AMERICA", -100, 45, "land"),
    ("SOUTH AMERICA", -60, -14, "land"),
    ("AFRICA", 20, 4, "land"),
    ("ASIA", 90, 50, "land"),
    ("AUSTRALIA", 134, -25, "land"),
    ("EUROPE", 15, 52, "land"),
    ("PACIFIC OCEAN", -140, 8, "ocean"),
    ("ATLANTIC OCEAN", -35, 12, "ocean"),
    ("INDIAN OCEAN", 78, -22, "ocean"),
    ("SOUTHERN OCEAN", 20, -60, "ocean"),
    ("Mediterranean Sea", 18, 34.5, "sea"),
    ("Caribbean Sea", -75, 15, "sea"),
    ("Gulf of Mexico", -90, 25, "sea"),
    ("South China Sea", 113, 12, "sea"),
    ("Arabian Sea", 65, 15, "sea"),
    ("Bay of Bengal", 88, 14, "sea"),
    ("Sea of Japan", 135, 40, "sea"),
    ("Philippine Sea", 133, 20, "sea"),
    ("Coral Sea", 155, -16, "sea"),
    ("Tasman Sea", 160, -38, "sea"),
    ("Banda Sea", 127, -6, "sea"),
    ("Bering Sea", -175, 58, "sea"),
    ("Sea of Okhotsk", 150, 55, "sea"),
    ("Black Sea", 34, 43, "sea"),
    ("North Sea", 3, 56, "sea"),
    ("Caspian Sea", 51, 42, "sea"),
)
BROAD_MIN_SPAN = 60  # continents and oceans show when the view spans at least this many degrees of latitude
SEA_SPAN = (30, 100)  # seas show between these spans (wide and region zoom, not the world or close)


def place_labels(
    view: Viewport, cols: int, rows: int, coast: BrailleCanvas, taken: Collection[tuple[int, int]] = ()
) -> dict[tuple[int, int], str]:
    """One character per cell for the labels that fit. A label needs its cells and a one-cell margin free of coast
    dots, of earlier labels and of `taken` cells (the markers)."""
    span = view.lat1 - view.lat0
    used: set[tuple[int, int]] = set(taken)
    out: dict[tuple[int, int], str] = {}
    for text, lon, lat, kind in LABELS:
        if kind == "sea":
            if not SEA_SPAN[0] <= span <= SEA_SPAN[1]:
                continue
        elif span < BROAD_MIN_SPAN:
            continue
        cell = project_cell(lon, lat, view, cols, rows)
        if cell is None:
            continue
        col0, row = cell[0] - len(text) // 2, cell[1]
        if col0 < 1 or col0 + len(text) > cols - 1:
            continue
        if any((c, row) in used or coast.cell_bits(c, row) for c in range(col0 - 1, col0 + len(text) + 1)):
            continue
        for i, ch in enumerate(text):
            out[(col0 + i, row)] = ch
        used.update((c, row) for c in range(col0 - 1, col0 + len(text) + 1))
    return out
