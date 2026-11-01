"""Every key and every clickable has a test that asserts an observable change, and a meta-test that fails when the
inventory (found by introspection in inventory.py) holds something with no case here.

A key case sets up the screen where the key is advertised, presses the key with pilot, and asserts on a before/after
snapshot of app state (screen, sort, cursor, map mode, pan, notifications, ...). A click case clicks the widget's
real region with pilot.click and asserts the result, and for lists that the selection survives the next refresh tick.
Footer labels are clicked for every key the footer shows, and must do what the key does.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from test_ui import mock_transport
from textual.widgets import DataTable, Input
from textual.widgets._footer import FooterKey

from earthwave.ui.screens.history import HistoryScreen
from earthwave.ui.widgets.map_panel import MapPanel
from earthwave.ui.widgets.quake_list import QuakeList
from uat import inventory, states
from uat.harness import kam_alert_app, screen_text, week_app

SIZE = (130, 32)
MAP_TITLE = re.compile(r"Map [·-] \w+")


@dataclass
class Snap:
    screen: str
    depth: int
    sort: str
    show_map: bool
    map_full: bool
    cursor: int
    map_mode: str
    pan: tuple[int, int]
    notes: int
    unseen: int
    focused: str
    search_open: bool
    day: object
    table_row: int
    text: str
    exited: bool

    @classmethod
    def take(cls, app) -> Snap:
        dash = app.dashboard
        screen = app.screen
        lists = list(screen.query(QuakeList))
        tables = list(screen.query(DataTable))
        model = dash.query_one(MapPanel).model
        return cls(
            screen=type(screen).__name__,
            depth=len(app.screen_stack),
            sort=app.sort.value,
            show_map=dash.show_map,
            map_full=dash.has_class("map-full"),
            cursor=lists[0].cursor if lists else -1,
            map_mode=model.mode.value,
            pan=(model.pan_lon, model.pan_lat),
            notes=len(app._notifications),
            unseen=app.tracker.unseen(dash._countable),
            focused=f"{type(app.focused).__name__}#{app.focused.id or ''}" if app.focused else "",
            search_open=bool(dash.query_one("#search").display),
            day=getattr(screen, "day", None),
            table_row=tables[0].cursor_row if tables else -1,
            text=screen_text(app),
            exited=not app.is_running or app._exit,
        )


@dataclass
class Case:
    expect: Callable[[Snap, Snap], bool]
    why: str
    app: str = "week"
    open: tuple[str, ...] = ()  # keys that reach the screen where the key is advertised
    pre: tuple[str, ...] = ()  # keys that put that screen in a state where the key can show an effect
    press: str | None = None
    xfail: str | None = None  # "#issue: reason", recorded before any fix
    sizes: tuple[int, int] = SIZE
    extra: dict = field(default_factory=dict)


def screen_is(name):
    return lambda b, a: a.screen == name


def back_to_dashboard(b, a):
    return a.screen == "DashboardScreen" and b.screen != "DashboardScreen"


# --- key cases ---------------------------------------------------------------------------------------------------

KEYS: dict[str, Case] = {}


def key(owner: str, name: str, case: Case) -> None:
    case.press = case.press or name
    KEYS[f"key:{owner}:{name}"] = case


D = "DashboardScreen"
key(D, "f", Case(screen_is("FiltersScreen"), "opens the filters dialog"))
key(D, "s", Case(lambda b, a: a.sort != b.sort and "sort:" + a.sort in a.text, "cycles the sort and the status bar says so"))
key(D, "/", Case(lambda b, a: a.search_open and a.focused == "Input#search", "opens the search box with focus", press="slash"))
key(D, "m", Case(lambda b, a: a.show_map != b.show_map and bool(MAP_TITLE.search(a.text)) != bool(MAP_TITLE.search(b.text)), "hides or shows the map"))
key(D, "M", Case(lambda b, a: a.map_full != b.map_full and "AGE  MAG" not in a.text, "toggles the full-screen map"))
key(D, "z", Case(lambda b, a: a.map_mode != b.map_mode and a.text != b.text, "cycles the map zoom and redraws"))
key(D, "t", Case(screen_is("TsunamiScreen"), "opens the tsunami screen"))
key(D, "o", Case(screen_is("ObservationsScreen"), "opens the gauges for the selected quake"))
key(D, "h", Case(screen_is("HistoryScreen"), "opens history by day"))
key(D, "a", Case(lambda b, a: b.unseen > 0 and a.unseen == 0, "acknowledges every unseen row", extra={"unseen": 3}))
key(D, "r", Case(lambda b, a: a.notes > b.notes, "refresh now says what it did"))
key(D, "?", Case(screen_is("HelpScreen"), "opens help", press="question_mark"))
key("EarthwaveApp", "q", Case(lambda b, a: a.exited, "quits from the dashboard"))
key("EarthwaveApp", "q:history", Case(lambda b, a: a.exited, "quits from the history screen", open=("h",), press="q"))
key("EarthwaveApp", "q:detail", Case(lambda b, a: a.exited, "quits from the detail screen", open=("enter",), press="q"))

L = "QuakeList"
key(L, "j", Case(lambda b, a: a.cursor == b.cursor + 1, "moves down one row"))
key(L, "down", Case(lambda b, a: a.cursor == b.cursor + 1, "moves down one row"))
key(L, "k", Case(lambda b, a: a.cursor == b.cursor - 1, "moves up one row", pre=("j", "j", "j")))
key(L, "up", Case(lambda b, a: a.cursor == b.cursor - 1, "moves up one row", pre=("j", "j", "j")))
key(L, "pagedown", Case(lambda b, a: a.cursor > b.cursor + 1, "moves down a page"))
key(L, "pageup", Case(lambda b, a: a.cursor < b.cursor - 1, "moves up a page", pre=("pagedown", "pagedown")))
key(L, "home", Case(lambda b, a: a.cursor == 0 and b.cursor > 0, "goes to the first row", pre=("j", "j", "j")))
key(L, "end", Case(lambda b, a: a.cursor > b.cursor + 5, "goes to the last row"))
key(L, "enter", Case(screen_is("DetailScreen"), "opens the quake detail"))

M = "MapPanel"
for k, d in (("left", "west"), ("right", "east"), ("up", "north"), ("down", "south")):
    key(M, k, Case(lambda b, a: a.pan != b.pan and a.text != b.text, f"pans the map {d}", pre=("z", "z", "tab")))

DT = "DetailScreen"
key(DT, "escape", Case(back_to_dashboard, "goes back", open=("enter",)))
key(DT, "b", Case(lambda b, a: a.extra_browser, "opens the USGS page in the browser", open=("enter",)))  # see run_key
key(DT, "y", Case(lambda b, a: a.notes > b.notes, "copies the URL and says so", open=("enter",)))

key("FiltersScreen", "escape", Case(back_to_dashboard, "cancels the dialog", open=("f",)))
key("HelpScreen", "escape", Case(back_to_dashboard, "closes help", open=("question_mark",)))
key("HelpScreen", "?", Case(back_to_dashboard, "closes help", open=("question_mark",), press="question_mark"))

H = "HistoryScreen"
key(H, "escape", Case(back_to_dashboard, "goes back to the dashboard", open=("h",)))
key(H, "h", Case(back_to_dashboard, "goes back to the dashboard", open=("h",)))
HL = "HistoryList"
key(HL, "enter", Case(screen_is("DetailScreen"), "opens the quake detail", open=("h",), app="history"))
key(HL, "left", Case(lambda b, a: a.day == b.day - timedelta(days=1), "shows the previous day", open=("h",)))
key(HL, "right", Case(lambda b, a: a.day == b.day + timedelta(days=1), "shows the next day", open=("h",), pre=("left",)))

OBS = "ObservationsScreen"
for k in ("escape", "o", "q"):
    key(OBS, k, Case(back_to_dashboard, "goes back to the dashboard", open=("o",)))

T = "TsunamiScreen"
for k in ("escape", "t", "q"):
    key(T, k, Case(back_to_dashboard, "goes back to the dashboard", open=("t",), app="kam"))
key(T, "j", Case(lambda b, a: a.table_row == b.table_row + 1, "moves down the alert table", open=("t",), app="kam"))
key(T, "k", Case(lambda b, a: a.table_row == b.table_row - 1, "moves up the alert table", open=("t",), pre=("j",), app="kam"))
key(T, "a", Case(lambda b, a: a.notes > b.notes, "acknowledges tsunami alerts and says so", open=("t",), app="kam"))

# keys handled by hand
key("DashboardScreen", "on_key:escape", Case(lambda b, a: b.search_open and not a.search_open, "closes the search box", open=("slash",), press="escape"))
key("DashboardScreen", "submit", Case(lambda b, a: b.search_open and not a.search_open, "applies the search", open=("slash", "z", "z"), press="enter"))
key("FiltersScreen", "submit", Case(back_to_dashboard, "Enter in a filter box applies and closes", open=("f",), press="enter"))

# help text promises
HELP_TAB = Case(lambda b, a: a.focused == "MapPanel#map", "Tab focuses the map so the arrows pan", press="tab")


def case_for_id(cid: str) -> Case | None:
    if cid in KEYS:
        return KEYS[cid]
    if cid.startswith("help:"):
        token = cid[5:]
        if token == "tab":
            return HELP_TAB
        return None
    return None


KEY_ID_ALIASES = {
    "key:EarthwaveApp:q:history": "",  # inventory id -> case id (same key, same behaviour)
    "key:DashboardScreen:slash": "key:DashboardScreen:/",
    "key:DashboardScreen:question_mark": "key:DashboardScreen:?",
    "key:HelpScreen:question_mark": "key:HelpScreen:?",
    "onkey:DashboardScreen:escape": "key:DashboardScreen:on_key:escape",
    "submit:DashboardScreen": "key:DashboardScreen:submit",
    "submit:FiltersScreen": "key:FiltersScreen:submit",
    "key:DashboardScreen:slash ": "",
}


def build(kind: str):
    if kind == "kam":
        app = kam_alert_app(0, 1, transport=mock_transport([]))
        app.history = states.FakeHistory()
    else:
        app = week_app(ticks=False, transport=mock_transport([]))
        app.history = states.FakeHistory(states.week_quakes())
    return app


async def reach(pilot, case: Case):
    for k in case.open:
        await pilot.press(k)
        await pilot.pause(0.15)
    for k in case.pre:
        await pilot.press(k)
        await pilot.pause(0.1)
    await pilot.pause(0.15)


async def run_key(case: Case, via: str, monkeypatch=None):
    app = build(case.app)
    opened = []
    if "DetailScreen" in case.why or case.why.startswith("opens the USGS"):
        import webbrowser

        monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: opened.append(url) or True)
    async with app.run_test(size=case.sizes) as pilot:
        await pilot.pause(0.2)
        if case.extra.get("unseen"):
            ids = [q.id for q in app.dashboard.query_one(QuakeList).rows[: case.extra["unseen"]]]
            app.tracker.restore(set(ids), set())
            app.dashboard.refresh_view()
        await reach(pilot, case)
        before = Snap.take(app)
        if via == "key":
            await pilot.press(case.press)
        else:  # click the footer label for this key
            label = next(k for k in app.screen.query(FooterKey) if inventory.norm(k.key) == inventory.norm(via))
            region = label.region
            await pilot.click(offset=(region.x + 1, region.y))
        await pilot.pause(0.4)
        after = Snap.take(app)
        after.extra_browser = bool(opened)  # type: ignore[attr-defined]
        return before, after


def _cases():
    for cid, case in KEYS.items():
        marks = [pytest.mark.xfail(strict=True, reason=case.xfail)] if case.xfail else []
        yield pytest.param(cid, case, id=cid, marks=marks)


@pytest.mark.parametrize("cid,case", _cases())
async def test_key_press_changes_observable_state(cid, case, monkeypatch):
    before, after = await run_key(case, "key", monkeypatch)
    assert case.expect(before, after), f"{cid} {case.why}: nothing observable happened\nbefore={before}\nafter={after}"


async def test_tab_focuses_the_map_as_the_help_text_says():
    app = build("week")
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        before = Snap.take(app)
        await pilot.press("tab")
        await pilot.pause(0.2)
        assert HELP_TAB.expect(before, Snap.take(app))


# --- footer labels clicked -----------------------------------------------------------------------------------------

FOOTER_CASE = {  # footer id -> key case id
    "footer:DashboardScreen:f": "key:DashboardScreen:f",
    "footer:DashboardScreen:s": "key:DashboardScreen:s",
    "footer:DashboardScreen:/": "key:DashboardScreen:/",
    "footer:DashboardScreen:m": "key:DashboardScreen:m",
    "footer:DashboardScreen:t": "key:DashboardScreen:t",
    "footer:DashboardScreen:o": "key:DashboardScreen:o",
    "footer:DashboardScreen:h": "key:DashboardScreen:h",
    "footer:DashboardScreen:a": "key:DashboardScreen:a",
    "footer:DashboardScreen:r": "key:DashboardScreen:r",
    "footer:DashboardScreen:?": "key:DashboardScreen:?",
    "footer:DashboardScreen:q": "key:EarthwaveApp:q",
    "footer:DetailScreen:escape": "key:DetailScreen:escape",
    "footer:DetailScreen:b": "key:DetailScreen:b",
    "footer:DetailScreen:y": "key:DetailScreen:y",
    "footer:DetailScreen:q": "key:EarthwaveApp:q:detail",
    "footer:HistoryScreen:q": "key:EarthwaveApp:q:history",
    "footer:HistoryScreen:escape": "key:HistoryScreen:escape",
    "footer:HistoryScreen:enter": "key:HistoryList:enter",
    "footer:HistoryScreen:left": "key:HistoryList:left",
    "footer:HistoryScreen:right": "key:HistoryList:right",
    "footer:ObservationsScreen:escape": "key:ObservationsScreen:escape",
    "footer:TsunamiScreen:escape": "key:TsunamiScreen:escape",
    "footer:TsunamiScreen:a": "key:TsunamiScreen:a",
}


@pytest.mark.parametrize("fid", list(FOOTER_CASE))
async def test_footer_label_click_does_what_the_key_does(fid, monkeypatch):
    case = KEYS[FOOTER_CASE[fid]]
    keyname = fid.split(":", 2)[2]
    before, after = await run_key(case, keyname, monkeypatch)
    assert case.expect(before, after), f"clicking the {fid} footer label: {case.why} did not happen"


# --- clicks on widgets ---------------------------------------------------------------------------------------------


async def test_click_a_quake_row_selects_it_and_it_sticks_through_refresh():
    app = week_app(ticks=True, transport=mock_transport([]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        ql = app.dashboard.query_one(QuakeList)
        rows = list(ql.rows)
        for target in (4, 9, 2):
            await pilot.click("#quakes", offset=(10, target))
            assert ql.cursor == target
            picked = ql.selected.id
            await pilot.pause(1.3)  # two ticks
            app.dashboard.refresh_view()
            await pilot.pause(0.2)
            assert ql.selected is not None and ql.selected.id == picked == rows[target].id
            assert app.screen is app.dashboard


async def test_click_below_the_last_row_selects_nothing_new():
    app = week_app(ticks=False, transport=mock_transport([]))
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.pause(0.2)
        ql = app.dashboard.query_one(QuakeList)
        app.dashboard.query_one(QuakeList).set_rows(ql.rows[:3])
        await pilot.click("#quakes", offset=(10, 1))
        await pilot.click("#quakes", offset=(10, 20))
        assert ql.cursor == 1


async def test_click_a_history_row_selects_it_and_it_sticks():
    app = build("week")
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        await pilot.press("h")
        await pilot.pause(0.6)
        assert isinstance(app.screen, HistoryScreen)
        ql = app.screen.query_one(QuakeList)
        assert len(ql.rows) >= 3
        await pilot.click("#history_list", offset=(10, 2))
        assert ql.cursor == 2
        picked = ql.selected.id
        await pilot.pause(0.8)
        app.screen.refresh_day()
        await pilot.pause(0.5)
        assert ql.selected is not None and ql.selected.id == picked


async def test_click_the_map_gives_it_focus_so_the_arrows_pan():
    app = build("week")
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        await pilot.press("z", "z")
        await pilot.click("#map", offset=(10, 5))
        await pilot.pause(0.2)
        assert isinstance(app.focused, MapPanel)
        before = Snap.take(app)
        await pilot.press("right")
        await pilot.pause(0.3)
        assert Snap.take(app).pan != before.pan


async def test_click_the_filter_buttons():
    for button, applied in (("#cancel", False), ("#apply", True)):
        app = build("week")
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause(0.2)
            await pilot.press("f")
            await pilot.pause(0.3)
            if applied:
                app.screen.query_one("#min_mag", Input).value = "6"
            await pilot.click(button)
            await pilot.pause(0.4)
            assert app.screen is app.dashboard
            assert (app.view.min_magnitude == 6.0) is applied


async def test_click_a_filter_input_focuses_it():
    app = build("week")
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        await pilot.press("f")
        await pilot.pause(0.3)
        await pilot.click("#hours")
        assert app.focused is not None and app.focused.id == "hours"
        await pilot.click("#min_mag")
        assert app.focused.id == "min_mag"


async def test_click_the_search_box_keeps_focus():
    app = build("week")
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        await pilot.press("slash")
        await pilot.pause(0.2)
        await pilot.click("#search")
        assert app.focused is not None and app.focused.id == "search"


async def test_click_a_tsunami_row_selects_it_and_shows_its_bulletin():
    app = build("kam")
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        await pilot.press("t")
        await pilot.pause(0.4)
        table = app.screen.query_one(DataTable)
        assert table.row_count == 2
        first = screen_text(app)
        await pilot.click("#alert_table", offset=(5, 2))  # row 1 (header is line 0)
        await pilot.pause(0.4)
        assert table.cursor_row == 1
        assert screen_text(app) != first, "the bulletin did not change with the selected row"
        await pilot.pause(1.2)
        assert table.cursor_row == 1


async def test_click_a_scroll_area_focuses_it():
    app = build("week")
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        await pilot.press("enter")
        await pilot.pause(0.5)
        await pilot.click("#detail_scroll", offset=(5, 3))
        assert app.focused is not None and app.focused.id == "detail_scroll"


@pytest.mark.parametrize(
    "opener,area", [("o", "observations_body"), ("t", "alert_text_scroll"), ("t", "gauges")]
)
async def test_click_the_other_scroll_areas_focus_them(opener, area):
    app = build("kam")
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        await pilot.press(opener)
        await pilot.pause(0.5)
        await pilot.click(f"#{area}", offset=(2, 1))
        await pilot.pause(0.2)
        assert app.focused is not None and app.focused.id == area


@pytest.mark.xfail(strict=True, reason="#229: double-clicking a quake row does not open its detail")
async def test_double_click_a_row_opens_detail():
    app = week_app(ticks=False, transport=mock_transport([]))
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        await pilot.click("#quakes", offset=(10, 3), times=2)
        await pilot.pause(0.4)
        assert type(app.screen).__name__ == "DetailScreen"


# --- the meta-test: nothing in the inventory is without a case ---------------------------------------------------

CLICK_CASES = {
    "click:QuakeList": "test_click_a_quake_row_selects_it_and_it_sticks_through_refresh",
    "click:HistoryList": "test_click_a_history_row_selects_it_and_it_sticks",
    "click:QuakeList#quakes": "test_click_a_quake_row_selects_it_and_it_sticks_through_refresh",
    "click:HistoryList#history_list": "test_click_a_history_row_selects_it_and_it_sticks",
    "click:MapPanel#map": "test_click_the_map_gives_it_focus_so_the_arrows_pan",
    "click:Button#apply": "test_click_the_filter_buttons",
    "click:Button#cancel": "test_click_the_filter_buttons",
    "click:Input#min_mag": "test_click_a_filter_input_focuses_it",
    "click:Input#hours": "test_click_a_filter_input_focuses_it",
    "click:Input#search": "test_click_the_search_box_keeps_focus",
    "click:DataTable#alert_table": "test_click_a_tsunami_row_selects_it_and_shows_its_bulletin",
    "click:VerticalScroll#detail_scroll": "test_click_a_scroll_area_focuses_it",
}
CLICK_CASES |= {
    f"click:VerticalScroll#{a}": "test_click_the_other_scroll_areas_focus_them"
    for a in ("observations_body", "alert_text_scroll", "gauges")
}
SCROLL_AREAS_NOT_CLICK_TESTED: set[str] = set()


async def _live_inventory() -> set[str]:
    found: set[str] = set()
    app = build("kam")
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        await pilot.press("slash")
        await pilot.pause(0.2)
        found |= inventory.live_widgets(app.screen)
        await pilot.press("escape")
        for k, _name in (("f", "filters"), ("question_mark", "help")):
            await pilot.press(k)
            await pilot.pause(0.3)
            found |= inventory.live_widgets(app.screen)
            await pilot.press("escape")
            await pilot.pause(0.2)
        for k in ("t", "o", "h"):
            await pilot.press(k)
            await pilot.pause(0.5)
            found |= inventory.live_widgets(app.screen)
            await pilot.press("escape")
            await pilot.pause(0.2)
        await pilot.press("enter")
        await pilot.pause(0.4)
        found |= inventory.live_widgets(app.screen)
    return found


def _covered_key_ids() -> set[str]:
    covered = set(KEYS)
    covered |= set(KEY_ID_ALIASES) | {"help:tab"}
    return covered


async def test_every_binding_and_handled_key_has_a_case():
    found = set(inventory.binding_keys()) | inventory.handler_keys()
    # the same key shown under two names, and keys that only exist as part of a bigger case
    aliases = {k: v for k, v in KEY_ID_ALIASES.items() if v}
    missing = sorted(i for i in found if i not in KEYS and i not in aliases)
    assert not missing, f"keys with no test case: {missing}"
    for alias_id, case_id in aliases.items():
        assert case_id in KEYS, f"{alias_id} points at a missing case {case_id}"
    assert len(found) >= 45, f"inventory looks too small: {len(found)}"


def test_every_key_the_help_text_names_is_a_real_binding_with_a_case():
    bindings = set(inventory.binding_keys())
    for token, keys in inventory.HELP_TOKEN_KEYS.items():
        if token not in inventory.help_tokens():
            continue
        for k in keys:
            if k == "tab":
                continue
            assert any(b.endswith(f":{inventory.norm(k)}") for b in bindings), f"help names {token!r}: nothing is bound to {k!r}"
    singles = {t for t in inventory.help_tokens() if len(t) == 1 and t not in "/?"}
    for t in singles:
        assert any(b.endswith(f":{t}") for b in bindings), f"help names {t!r} but nothing is bound to it"
    assert "Tab" in inventory.help_tokens()


def test_every_footer_key_is_in_the_click_table():
    static = inventory.shown_footer_bindings()
    assert static == set(FOOTER_CASE), (static ^ set(FOOTER_CASE))


async def test_the_footer_shown_live_is_the_footer_the_bindings_declare():
    live: set[str] = set()
    app = build("kam")
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.2)
        for opener in ((), ("enter",), ("t",), ("o",), ("h",)):
            for k in opener:
                await pilot.press(k)
                await pilot.pause(0.5)
            live |= {i for i in inventory.live_widgets(app.screen) if i.startswith("footer:")}
            if opener:
                await pilot.press("escape")
                await pilot.pause(0.3)
    assert live == set(FOOTER_CASE), (sorted(live - set(FOOTER_CASE)), sorted(set(FOOTER_CASE) - live))


async def test_every_clickable_widget_has_a_click_test():
    live = {i for i in await _live_inventory() if i.startswith("click:")}
    live -= SCROLL_AREAS_NOT_CLICK_TESTED
    unfocusable_noise = {i for i in live if i.startswith(("click:Footer#", "click:FooterKey"))}
    live -= unfocusable_noise
    missing = sorted(i for i in live if i not in CLICK_CASES)
    assert not missing, f"clickable widgets with no click test: {missing}"
    for name in set(CLICK_CASES.values()):
        assert name in globals(), f"click case {name} does not exist"
    classes = inventory.click_classes()
    assert classes <= set(CLICK_CASES), sorted(classes - set(CLICK_CASES))
    assert inventory.markup_click_actions() == [], "a markup click/link appeared; it needs a test"
    print(f"\nclickables found live: {len(live)} (+{len(SCROLL_AREAS_NOT_CLICK_TESTED)} scroll areas), "
          f"footer labels {len(FOOTER_CASE)}, on_click classes {len(classes)}")  # fmt: skip


async def test_print_counts():
    keys = set(inventory.binding_keys()) | inventory.handler_keys()
    print(f"\nkey ids found: {len(keys)}; cases: {len(KEYS)}; help tokens: {len(inventory.help_tokens())}; "
          f"footer labels: {len(FOOTER_CASE)}")
    await asyncio.sleep(0)
