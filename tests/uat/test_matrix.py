"""State matrix: nine app states x seven screens x four sizes, with a rule list that asserts no contradictions.

A combination passes when every rule holds. A combination that breaks a rule we have filed carries
`xfail(strict=True)` with the issue number in KNOWN, so it is recorded before any fix and turns red when fixed.
Combinations that cannot exist (no quake to open a detail screen on) are counted as n/a, not as passes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pytest
from conftest import wait_until
from textual.widgets._footer import FooterKey

from earthwave.sources import levels
from earthwave.ui.widgets.map_panel import MapPanel
from earthwave.ui.widgets.quake_list import QuakeList
from uat import states
from uat.harness import screen_text

MATRIX_SIZES = [(100, 30), (120, 30), (200, 50), (280, 70)]
SCREENS = ("dashboard", "help", "filters", "tsunami", "detail", "observations", "history")
OPEN_KEY = {"help": "?", "filters": "f", "tsunami": "t", "detail": "enter", "observations": "o", "history": "h"}
MODAL = {"help", "filters"}
NEEDS_QUAKE = {"detail", "observations"}
HAS_QUAKES = {"loading", "stale", "busy", "quiet", "offline", "replay"}

# (state, screen) -> issue number, for a rule violation that is filed and not yet fixed. Filled from real findings.
KNOWN: dict[tuple[str, str, int | None], tuple[int, str]] = {
    ("stale", "tsunami", None): (227, "tsunami screen says no products received while the feeds are down"),
    ("error", "tsunami", None): (227, "tsunami screen says no products received while the feeds are down"),
    ("loading", "dashboard", 100): (228, "status bar clips the failing feed names at 100 columns"),
    ("stale", "dashboard", 100): (228, "status bar clips the failing feed names at 100 columns"),
}


def combos():
    for state in states.STATES:
        for screen in SCREENS:
            if screen in NEEDS_QUAKE and state not in HAS_QUAKES:
                continue
            for size in MATRIX_SIZES:
                marks = []
                known = KNOWN.get((state, screen, size[0])) or KNOWN.get((state, screen, None))
                if known:
                    issue, why = known
                    marks.append(pytest.mark.xfail(strict=True, reason=f"#{issue}: {why}"))
                yield pytest.param(state, screen, size, id=f"{state}-{screen}-{size[0]}x{size[1]}", marks=marks)


TOTAL_POSSIBLE = len(states.STATES) * len(SCREENS) * len(MATRIX_SIZES)


@dataclass
class Ctx:
    state: str
    screen: str
    size: tuple[int, int]
    app: object
    text: str

    @property
    def lines(self) -> list[str]:
        return self.text.splitlines()

    @property
    def status(self) -> str:
        return next((ln for ln in self.lines if " shown of " in ln), "")


def r_status_count_matches_list(c: Ctx) -> str | None:
    if c.screen != "dashboard":
        return None
    shown = re.search(r"(\d+) shown of (\d+)", c.text)
    rows = c.app.dashboard.query_one(QuakeList).rows
    if not shown:
        return "no 'N shown of M' in the status bar"
    if int(shown[1]) != len(rows):
        return f"status says {shown[1]} shown, the list holds {len(rows)}"
    if int(shown[1]) > int(shown[2]):
        return f"status says {shown[1]} shown of {shown[2]}"
    on_screen = sum(1 for ln in c.lines if re.match(r"^[▌> ]\s*\d+[smhd]\s+\d\.\d", ln))
    height = c.app.dashboard.query_one(QuakeList).size.height
    if on_screen != min(len(rows), height):
        return f"{len(rows)} rows ({height} fit) but {on_screen} drawn"
    return None


def r_feeds_ok_matches_health(c: Ctx) -> str | None:
    if c.screen != "dashboard":
        return None
    app = c.app
    bad = [n for n in app.scheduler.statuses if app.scheduler.freshness(n).value != "fresh"] if app.scheduler_running else []
    if "feeds ok" in c.status and bad:
        return f"'feeds ok' while {bad} are not fresh"
    if bad and not any(w in c.status for w in ("loading", "stale", "failed", "incomplete")):
        return f"feeds {bad} are not fresh but the status bar names no problem: {c.status!r}"
    return None


def r_status_names_every_unhealthy_feed(c: Ctx) -> str | None:
    if c.screen != "dashboard" or not c.app.scheduler_running:
        return None
    app = c.app
    bad = [n for n in app.scheduler.statuses if app.scheduler.freshness(n).value != "fresh"]
    hidden = [n for n in bad if n not in c.status]
    return f"status bar does not name {hidden} (clipped at {c.size[0]} columns?): {c.status!r}" if hidden else None


def r_tsunami_text_matches_alerts(c: Ctx) -> str | None:
    if c.screen != "dashboard":
        return None
    app = c.app
    active = app.store.active_alerts(app.clock)
    if active and "No tsunami alerts in force" in c.text:
        return "'No tsunami alerts in force' with an alert in force"
    if not active and not app.offline and "No tsunami alerts in force" not in c.text:
        feeds_down = app.tsunami_feeds_down()[0] or app.tsunami_feeds_loading()
        if not feeds_down:
            return "no alerts and every tsunami feed fresh, but the panel does not say none are in force"
    if active and any(a.level in levels.ACTIONABLE for a in active):
        if "TSUNAMI" not in c.status or "TSUNAMI" not in "\n".join(c.lines[:6]):
            return "an actionable alert but the banner or status bar does not say TSUNAMI"
    elif "TSUNAMI" in c.text and not active:
        return "TSUNAMI shown with no alert in force"
    return None


def r_offline_vs_live(c: Ctx) -> str | None:
    app = c.app
    low = c.text.lower()
    if app.offline and ("feeds ok" in low or "next 0:" in low or "waiting for usgs" in low):
        return "offline but the screen talks about live feeds"
    if app.offline and c.screen == "dashboard" and "offline" not in c.status.lower():
        return "offline but the status bar does not say so"
    if not app.offline and c.screen == "dashboard" and "offline" in c.status.lower():
        return "status bar says offline in an online run"
    return None


def r_empty_message_matches_list(c: Ctx) -> str | None:
    if c.screen != "dashboard":
        return None
    rows = c.app.dashboard.query_one(QuakeList).rows
    empty_text = ("No quakes match", "Waiting for USGS", "USGS is not answering", "Quakes are not being fetched")
    has_msg = any(m in c.text for m in empty_text)
    if rows and has_msg:
        return "an empty-list message above a non-empty list"
    if not rows and not has_msg:
        return "an empty list with no reason shown"
    return None


def r_map_title_matches_view(c: Ctx) -> str | None:
    if c.screen != "dashboard":
        return None
    dash = c.app.dashboard
    title = re.search(r"Map [·-] (\w+)", c.text)
    wide = c.size[0] >= 120
    if wide and not title:
        return "no map title at a width that shows the map"
    if not wide and title:
        return "a map title at a width that hides the map"
    shown = dash.query_one(MapPanel).model.shown.split()[0]
    if title and title[1] != shown:
        return f"map title {title[1]!r} but the map is drawn at {shown!r}"
    return None


def r_footer_lists_every_advertised_key(c: Ctx) -> str | None:
    last = c.lines[-1] if c.lines else ""
    missing = [k.description for k in c.app.screen.query(FooterKey) if k.region.width and k.description not in last]
    return f"footer is missing {missing}: {last!r}" if missing else None


def r_no_placeholder_leaks(c: Ctx) -> str | None:
    hit = re.search(r"\bNone\b|\bnan\b|\bNaN\b|Traceback|\{\w*\}|%[sd]", c.text)
    return f"placeholder or error text on screen: {hit[0]!r}" if hit else None


def r_detail_matches_selection(c: Ctx) -> str | None:
    if c.screen != "detail":
        return None
    quake = c.app.screen.quake
    sel = c.app.dashboard.query_one(QuakeList).selected
    if sel is None or quake.id != sel.id:
        return f"detail is for {quake.id}, the selected row is {sel.id if sel else None}"
    if f"M{quake.mag:.1f}" not in c.text or (quake.place and quake.place not in c.text):
        return "detail screen does not show the selected quake's magnitude and place"
    return None


def r_observations_matches_selection(c: Ctx) -> str | None:
    if c.screen != "observations":
        return None
    sel = c.app.dashboard.query_one(QuakeList).selected
    return None if sel and f"M{sel.mag:.1f}" in c.text else "observations header is not the selected quake"


def r_tsunami_screen_matches_store(c: Ctx) -> str | None:
    if c.screen != "tsunami":
        return None
    app = c.app
    alerts = app.store.alerts()
    if alerts and "No tsunami products received yet" in c.text:
        return "'No tsunami products received yet' with alerts in the store"
    if not alerts and "No tsunami products received yet" not in c.text:
        return "no alerts in the store but no empty message"
    if not alerts and app.scheduler_running:
        down, _ = app.tsunami_feeds_down()
        if down and not any(w in c.text for w in ("not answering", "failed", "stale", "UNKNOWN")):
            return f"tsunami feeds {[n for n, _ in down]} are down but the tsunami screen only says no products received"
    return None


def r_history_matches_list(c: Ctx) -> str | None:
    if c.screen != "history":
        return None
    rows = sum(1 for ln in c.lines if re.match(r"^[▌> ]\s*\d+[smhd]\s+\d\.\d", ln))
    stat = re.search(r"(\d+) quakes?", c.text)
    shown = len(c.app.screen.query_one(QuakeList).rows)
    if "History " not in c.text:
        return "history screen has no day heading"
    if stat and int(stat[1]) != shown:
        return f"history stats say {stat[1]} quakes, the list holds {shown}"
    return None if rows <= shown else "more rows drawn than the list holds"


RULES = [
    r_status_count_matches_list,
    r_feeds_ok_matches_health,
    r_status_names_every_unhealthy_feed,
    r_tsunami_text_matches_alerts,
    r_offline_vs_live,
    r_empty_message_matches_list,
    r_map_title_matches_view,
    r_footer_lists_every_advertised_key,
    r_no_placeholder_leaks,
    r_detail_matches_selection,
    r_observations_matches_selection,
    r_tsunami_screen_matches_store,
    r_history_matches_list,
]


def test_the_matrix_is_the_size_we_report():
    n = sum(1 for _ in combos())
    print(f"\nmatrix: {n} combinations; {TOTAL_POSSIBLE - n} n/a (detail and observations need a quake)")
    assert n == TOTAL_POSSIBLE - len(NEEDS_QUAKE) * len(MATRIX_SIZES) * (len(states.STATES) - len(HAS_QUAKES))


@pytest.mark.parametrize("state,screen,size", combos())
async def test_matrix_combination(state, screen, size):
    app = states.build(state)
    async with app.run_test(size=size) as pilot:
        await pilot.pause(0.1)
        states.stage(app, state)
        await pilot.pause(0.1)
        if screen != "dashboard":
            await pilot.press(OPEN_KEY[screen])
            await wait_until(pilot, lambda: type(app.screen).__name__.lower().startswith(screen[:4]), 3.0, screen)
            await pilot.pause(0.3)
            await wait_until(pilot, lambda: bool(list(app.screen.query(FooterKey))) or screen in MODAL, 3.0, "footer")
        await pilot.pause(0.2)
        ctx = Ctx(state, screen, size, app, screen_text(app))
        violations = [msg for rule in RULES if (msg := rule(ctx))]
        assert not violations, f"{state}/{screen}/{size[0]}x{size[1]}:\n  " + "\n  ".join(violations) + "\n" + ctx.text
