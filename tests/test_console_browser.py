"""`b` with a console browser (w3m, lynx, links: what `webbrowser` picks over ssh with no display) hands the terminal
over: the app suspends around it and resumes after, instead of drawing beside it with the keyboard dead (#246)."""

import webbrowser
from contextlib import contextmanager

import pytest
from conftest import wait_until
from test_ui import week_app
from textual.app import App, SuspendNotSupported

from earthwave.ui.screens.detail import DetailScreen, is_console_browser


class FakeTextBrowser(webbrowser.GenericBrowser):
    """Blocks like w3m: open() returns when the 'browser' exits."""

    def __init__(self, log):
        super().__init__("fake-text-browser")
        self.log = log

    def open(self, url, new=0, autoraise=True):
        self.log.append(("open", url))
        return True


@pytest.mark.parametrize(
    ("browser", "console"),
    [
        (webbrowser.GenericBrowser("w3m"), True),
        (webbrowser.Elinks("elinks"), True),
        (webbrowser.BackgroundBrowser("firefox"), False),
        (webbrowser.Chrome("chrome"), False),
    ],
)
def test_text_browsers_are_told_from_graphical_ones(monkeypatch, browser, console):
    monkeypatch.setattr(webbrowser, "get", lambda *a, **k: browser)
    assert is_console_browser() is console


def test_no_browser_at_all_is_not_a_console_browser(monkeypatch):
    def none(*a, **k):
        raise webbrowser.Error("could not locate runnable browser")

    monkeypatch.setattr(webbrowser, "get", none)
    assert is_console_browser() is False


async def open_quake(app, pilot):
    app.open_detail(app.store.visible(app.view, app.clock)[0])
    await wait_until(pilot, lambda: isinstance(app.screen, DetailScreen), what="the detail screen")
    return app.screen.quake.url


async def test_console_browser_runs_with_the_app_suspended_and_the_app_survives(monkeypatch):
    log: list = []
    monkeypatch.setattr(webbrowser, "get", lambda *a, **k: FakeTextBrowser(log))
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: FakeTextBrowser(log).open(url))

    @contextmanager
    def suspend(self):
        log.append(("suspend",))
        yield
        log.append(("resume",))

    monkeypatch.setattr(App, "suspend", suspend)
    app = week_app()
    async with app.run_test(size=(120, 40)) as pilot:
        url = await open_quake(app, pilot)
        await pilot.press("b")
        await pilot.pause()
        assert log == [("suspend",), ("open", url), ("resume",)]
        await pilot.press("escape")  # keys still reach the app afterwards
        await wait_until(pilot, lambda: app.screen is app.dashboard, what="going back")


async def test_console_browser_where_the_terminal_cannot_be_handed_over_shows_the_url(monkeypatch):
    log: list = []
    monkeypatch.setattr(webbrowser, "get", lambda *a, **k: FakeTextBrowser(log))
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: FakeTextBrowser(log).open(url))

    @contextmanager
    def cannot(self):
        raise SuspendNotSupported("no")
        yield

    monkeypatch.setattr(App, "suspend", cannot)
    app = week_app()
    notes: list[str] = []
    async with app.run_test(size=(120, 40)) as pilot:
        monkeypatch.setattr(app, "notify", lambda message, **k: notes.append(message))
        url = await open_quake(app, pilot)
        await pilot.press("b")
        await pilot.pause()
        assert log == []  # nothing was started beside the running app
        assert notes and url in notes[0]
