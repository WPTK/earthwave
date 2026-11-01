"""Map labels: few, prioritised, dropped (never moved) on collision, and only in empty cells."""

from earthwave.ui.widgets.braille import coastline_canvas
from earthwave.ui.widgets.map_labels import LABELS, place_labels
from earthwave.ui.widgets.map_panel import world_view, zoom_view


def words(labels: dict[tuple[int, int], str]) -> list[str]:
    out: list[str] = []
    for row in sorted({r for _, r in labels}):
        cols = sorted(c for c, r in labels if r == row)
        run = ""
        for i, c in enumerate(cols):
            if i and c != cols[i - 1] + 1:
                out.append(run)
                run = ""
            run += labels[(c, row)]
        out.append(run)
    return out


def test_world_view_labels_continents_and_oceans_but_no_seas():
    view = world_view(0, 200, 50)
    shown = words(place_labels(view, 200, 50, coastline_canvas(200, 50, view)))
    assert {"AFRICA", "NORTH AMERICA", "PACIFIC OCEAN"} <= set(shown)
    assert not any(t.endswith("Sea") for t in shown)


def test_region_view_labels_seas_only_and_close_view_none():
    view = zoom_view(115, -2, 40, 200, 50)
    shown = words(place_labels(view, 200, 50, coastline_canvas(200, 50, view)))
    assert "Banda Sea" in shown and not any(t.isupper() for t in shown)
    close = zoom_view(115, -2, 16, 200, 50)
    assert place_labels(close, 200, 50, coastline_canvas(200, 50, close)) == {}


def test_labels_keep_clear_of_coast_and_a_marked_cell_drops_its_label():
    view = world_view(0, 200, 50)
    coast = coastline_canvas(200, 50, view)
    labels = place_labels(view, 200, 50, coast)
    assert labels and not any(coast.cell_bits(c, r) for c, r in labels)
    cell = next(iter(labels))
    after = place_labels(view, 200, 50, coast, taken={cell})
    assert cell not in after and len(words(after)) >= len(words(labels)) - 1


def test_label_names_are_unique_and_placement_is_deterministic():
    view = zoom_view(-75, 20, 90, 200, 50)
    coast = coastline_canvas(200, 50, view)
    assert place_labels(view, 200, 50, coast) == place_labels(view, 200, 50, coast)
    assert len({t for t, *_ in LABELS}) == len(LABELS)
