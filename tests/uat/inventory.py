"""Input inventory by introspection: every key binding, every key named in the help text, every footer key and every
clickable thing in earthwave, found from the code and the running widget tree rather than from a hand-kept list.

`test_inventory.py` fails when something found here has no test case. Ids look like:
  key:<OwnerClass>:<key>          a BINDINGS entry (a comma list becomes one id per key)
  onkey:<OwnerClass>:<key>        a key handled in an on_key method (found with ast)
  submit:<OwnerClass>             an Input.Submitted handler (Enter in a text box)
  help:<Token>                    a key named in the help screen text
  footer:<Screen>:<key>           a key shown in the footer of a screen (clickable label)
  click:<Type>#<id>               a clickable widget in the running tree (focusable, Button, row list, footer key)
  click:<Class>                   a class that defines on_click
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import re
from pathlib import Path

from textual.binding import Binding
from textual.widget import Widget

import earthwave
from earthwave.app import EarthwaveApp
from earthwave.ui.screens.dashboard import DashboardScreen
from earthwave.ui.screens.detail import DetailScreen
from earthwave.ui.screens.filters import FiltersScreen
from earthwave.ui.screens.help import HELP_TEXT, HelpScreen
from earthwave.ui.screens.history import HistoryList, HistoryScreen
from earthwave.ui.screens.observations import ObservationsScreen
from earthwave.ui.screens.tsunami import TsunamiScreen
from earthwave.ui.widgets.map_panel import MapPanel
from earthwave.ui.widgets.quake_list import QuakeList

KEY_OWNERS = [
    EarthwaveApp, DashboardScreen, DetailScreen, FiltersScreen, HelpScreen, HistoryScreen, HistoryList,
    ObservationsScreen, TsunamiScreen, QuakeList, MapPanel,
]  # fmt: skip
SCREENS = [DashboardScreen, DetailScreen, FiltersScreen, HelpScreen, HistoryScreen, ObservationsScreen, TsunamiScreen]
KEY_NAMES = {"slash": "/", "question_mark": "?"}
HELP_TOKEN_KEYS = {  # what each token in the help text promises, as binding keys
    "j/k, arrows": ("j", "k", "up", "down"),
    "Enter": ("enter",),
    "/": ("slash",),
    "?": ("question_mark",),
    "Tab": ("tab",),
    "Esc": ("escape",),
}


def norm(key: str) -> str:
    return KEY_NAMES.get(key, key)


def all_bindings(cls) -> list[Binding]:
    out = []
    for entry in cls.__dict__.get("BINDINGS", []):
        out.append(entry if isinstance(entry, Binding) else Binding(*entry))
    return out


def binding_keys() -> dict[str, Binding]:
    found: dict[str, Binding] = {}
    for cls in KEY_OWNERS:
        for binding in all_bindings(cls):
            for key in binding.key.split(","):
                found[f"key:{cls.__name__}:{norm(key.strip())}"] = binding
    return found


def _package_files() -> list[Path]:
    root = Path(earthwave.__file__).parent
    return list(root.rglob("*.py"))


def handler_keys() -> set[str]:
    """Keys handled by hand in an on_key method, and Enter in text boxes (on_input_submitted)."""
    found: set[str] = set()
    for path in _package_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
                if fn.name == "on_input_submitted":
                    found.add(f"submit:{cls.name}")
                if fn.name == "on_key":
                    for node in ast.walk(fn):
                        if (
                            isinstance(node, ast.Compare)
                            and isinstance(node.left, ast.Attribute)
                            and node.left.attr == "key"
                            and isinstance(node.comparators[0], ast.Constant)
                        ):
                            found.add(f"onkey:{cls.name}:{node.comparators[0].value}")
    return found


def help_tokens() -> dict[str, str]:
    """Token -> description for every key named in the Keys section of the help text."""
    section = HELP_TEXT.split("[b]Colors[/b]")[0]
    tokens: dict[str, str] = {}
    for line in section.splitlines()[1:]:
        if line.strip().startswith("Detail:"):
            tokens.update({"b": "open page", "y": "copy URL", "Esc": "back"})
            continue
        parts = re.split(r"\s{2,}", line.strip())
        for key, desc in zip(parts[0::2], parts[1::2], strict=False):
            tokens[key] = desc
    return tokens


def help_keys() -> set[str]:
    out = set()
    for token in help_tokens():
        for key in HELP_TOKEN_KEYS.get(token, (token.lower() if len(token) == 1 else token,)):
            out.add(f"help:{norm(key) if key in KEY_NAMES else key}")
    return out


def shown_footer_bindings() -> set[str]:
    """footer:<Screen>:<key> for every binding with show=True on a screen, its focused widget and the app."""
    footer_owners = {
        DashboardScreen: [DashboardScreen],
        DetailScreen: [DetailScreen],
        HistoryScreen: [HistoryScreen, HistoryList],
        ObservationsScreen: [ObservationsScreen],
        TsunamiScreen: [TsunamiScreen],
    }
    found = set()
    for screen, owners in footer_owners.items():
        for owner in owners:
            for binding in all_bindings(owner):
                if binding.show:
                    found.add(f"footer:{screen.__name__}:{norm(binding.key.split(',')[0])}")
    # the app's q is shown wherever no screen binding for q hides it
    for screen in (DashboardScreen, DetailScreen, HistoryScreen):
        found.add(f"footer:{screen.__name__}:q")
    return found


def click_classes() -> set[str]:
    """Classes in the package that define their own on_click, plus their subclasses."""
    found = set()
    for info in pkgutil.walk_packages(earthwave.__path__, "earthwave."):
        if info.name.endswith(("__main__",)):
            continue
        module = importlib.import_module(info.name)
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ == module.__name__ and issubclass(cls, Widget) and callable(getattr(cls, "on_click", None)):
                found.add(f"click:{cls.__name__}")
    found.add("click:HistoryList")  # inherits QuakeList.on_click
    return found


def markup_click_actions() -> list[str]:
    """`[@click=...]` links in strings anywhere in the package (none today; if one appears it needs a test)."""
    hits = []
    for path in _package_files():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "@click" in line or "link=" in line:
                hits.append(f"{path.name}:{n}")
    return hits


def live_widgets(screen) -> set[str]:
    """click:<Type>#<id> for everything on a mounted screen the mouse can act on: anything that takes focus, buttons
    and footer keys."""
    from textual.widgets import Button
    from textual.widgets._footer import FooterKey

    found = set()
    for widget in screen.query("*"):
        if isinstance(widget, FooterKey):
            found.add(f"footer:{type(screen).__name__}:{norm(widget.key)}")
        elif (widget.can_focus or isinstance(widget, Button)) and widget.display and widget.region.area:
            found.add(f"click:{type(widget).__name__}#{widget.id or ''}")
    return found
