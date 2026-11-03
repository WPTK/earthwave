"""Detail screen: P/S arrival countdown (home only) and energy context (always), at three terminal sizes."""

from datetime import timedelta

import pytest
from conftest import T0, make_quake
from test_ui import wait_for, week_app
from uat.harness import screen_text

from earthwave.models import Earthquake
from earthwave.seismo import fmt_tnt, tnt_tons
from earthwave.ui.screens.detail import DetailScreen, render_detail
from earthwave.ui.widgets.quake_list import QuakeList

HOME = (40.7, -74.0)  # New York
SIZES = [(80, 24), (120, 40), (200, 60)]


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("home", [True, False])
async def test_detail_shows_waves_only_with_home_and_energy_always(size, home):
    app = week_app(offline=True, home=home)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        top = app.dashboard.query_one(QuakeList).rows[0]
        await pilot.press("enter")
        screen = app.screen
        assert isinstance(screen, DetailScreen)
        if size == (80, 24):  # below 100x30 the app shows its overlay instead of the screen; the content is still there
            await wait_for(lambda: "Terminal too small: 80x24" in screen_text(app))
            shown = str(screen.query_one("#detail").render())
        else:
            await wait_for(lambda: "Energy" in screen_text(app))
            shown = screen_text(app)
        assert f"~{fmt_tnt(tnt_tons(top.mag))} TNT" in shown
        assert "31.6x energy" in shown
        if home:
            assert "P wave" in shown and "S wave" in shown and "arrived" in shown  # the week-old quake is long past
        else:
            assert "P wave" not in shown and "S wave" not in shown and "arrived" not in shown


def detail_text(quake: Earthquake, now, home=HOME) -> str:
    return render_detail(quake, None, "", home, "UTC", now).plain


def test_the_countdown_runs_until_each_wave_arrives():
    quake = make_quake(lat=41.7, lon=-74.0, depth_km=10.0, mag=4.0, time=T0)  # about 111 km from home
    early = detail_text(quake, T0 + timedelta(seconds=5))
    assert "P wave" in early and "S wave" in early and "arrived" not in early and early.count("(in 0:") == 2
    between = detail_text(quake, T0 + timedelta(seconds=25))  # P about 19 s, S about 33 s
    assert between.count("arrived") == 1
    assert detail_text(quake, T0 + timedelta(minutes=5)).count("arrived") == 2


def test_a_quake_at_home_arrives_at_the_origin_time():
    quake = make_quake(lat=HOME[0], lon=HOME[1], depth_km=0.0, time=T0)
    assert detail_text(quake, T0 - timedelta(seconds=1)).count("(in 0:01)") == 2
    assert detail_text(quake, T0).count("arrived") == 2


def test_no_home_no_wave_rows_but_energy_stays():
    text = detail_text(make_quake(mag=7.0), T0, home=None)
    assert "P wave" not in text and "Energy" in text and "477 kt TNT" in text
