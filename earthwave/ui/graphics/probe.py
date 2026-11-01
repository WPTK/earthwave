"""Terminal probe and the `map_renderer` decision: sixel only when the terminal says it can, braille otherwise.

The probe sends three queries and reads the answers before Textual takes over the terminal:

- `CSI 16 t` -> `CSI 6 ; height ; width t`   pixel size of one cell
- `CSI 14 t` -> `CSI 4 ; height ; width t`   pixel size of the text area (used when 16 is not answered)
- `CSI c`    -> `CSI ? 61 ; ... ; 4 ; ... c` primary device attributes; attribute 4 is sixel graphics

Every terminal answers the last one, so it ends the wait; a terminal that does not answer within `TIMEOUT` seconds is
treated as having no graphics. Nothing here assumes: `decide` is pure and takes the environment and the probe as
arguments.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

log = logging.getLogger(__name__)

QUERY = "\x1b[16t\x1b[14t\x1b[c"
TIMEOUT = 0.2  # seconds to wait for the device attributes reply
FALLBACK_CELL = (8, 16)  # pixels, used only by a forced `sixel` when the terminal does not report its cell size
SANE_CELL = ((4, 64), (8, 128))  # (width, height) bounds; anything else is a misread reply, not a cell size

_DA1 = re.compile(r"\x1b\[\?([\d;]*)c")
_CELL = re.compile(r"\x1b\[6;(\d+);(\d+)t")
_AREA = re.compile(r"\x1b\[4;(\d+);(\d+)t")


@dataclass(frozen=True, slots=True)
class ProbeReply:
    sixel: bool
    cell: tuple[int, int] | None  # (width, height) in pixels


@dataclass(frozen=True, slots=True)
class GraphicsChoice:
    renderer: str  # "braille" | "sixel"
    cell: tuple[int, int] | None  # pixels per cell, set for sixel
    reason: str


BRAILLE = GraphicsChoice("braille", None, "default")


def _sane(width: int, height: int) -> tuple[int, int] | None:
    (w_lo, w_hi), (h_lo, h_hi) = SANE_CELL
    return (width, height) if w_lo <= width <= w_hi and h_lo <= height <= h_hi else None


def parse_reply(text: str, cols: int = 0, rows: int = 0) -> ProbeReply | None:
    """Read the probe answers; None when no device attributes reply is in `text` (the terminal did not answer)."""
    da1 = _DA1.search(text)
    if da1 is None:
        return None
    sixel = "4" in da1.group(1).split(";")
    cell = None
    if m := _CELL.search(text):
        cell = _sane(int(m.group(2)), int(m.group(1)))
    elif (m := _AREA.search(text)) and cols > 0 and rows > 0:
        cell = _sane(int(m.group(2)) // cols, int(m.group(1)) // rows)
    return ProbeReply(sixel, cell)


class CellReply:
    """Reads a `CSI 6 ; height ; width t` reply (the answer to `CSI 16 t`) one character at a time.

    After startup the terminal's answers come in on Textual's own input stream, as key events (Windows Terminal
    delivers the escape as a literal `^`), so a font-size change can be re-probed without a second reader on stdin.
    `feed` returns (consumed, cell): `consumed` is True for characters that belong to the reply, `cell` is set when
    the reply is complete. Anything that does not look like the reply is passed on as typed.
    """

    __slots__ = ("buffer", "replies", "state")

    def __init__(self) -> None:
        self.replies = 0  # complete replies read so far
        self.state = 0  # 0 waiting for the escape, 1 got it, 2 reading the parameters
        self.buffer = ""

    def feed(self, char: str) -> tuple[bool, tuple[int, int] | None]:
        if self.state == 0:
            if char in ("^", "\x1b"):
                self.state = 1
                return True, None
            if char == "[":  # the console input does not always deliver the escape itself
                self.state, self.buffer = 2, ""
                return True, None
            return False, None
        if self.state == 1:
            if char == "[":
                self.state = 2
                return True, None
            self.state = 0
            return False, None
        if char.isdigit() or char == ";":
            self.buffer += char
            return True, None
        done = char == "t" and (m := re.fullmatch(r"6;(\d+);(\d+)", self.buffer))
        cell = _sane(int(m.group(2)), int(m.group(1))) if done else None
        self.state, self.buffer = 0, ""
        if done:
            self.replies += 1
        return bool(done), cell


def _multiplexer(environ: Mapping[str, str]) -> bool:
    term = environ.get("TERM", "")
    return bool(environ.get("TMUX") or environ.get("STY")) or term.startswith(("screen", "tmux"))


def decide(
    setting: str,
    environ: Mapping[str, str],
    is_tty: bool,
    size: tuple[int, int],
    query: Callable[[], str],
) -> GraphicsChoice:
    """The renderer for `setting` (auto | braille | sixel). `query` runs the probe and returns the raw reply text;
    it is only called once every cheap reason to stay on braille has been ruled out."""
    if setting == "braille":
        return GraphicsChoice("braille", None, "map_renderer is braille")
    if not is_tty:
        return GraphicsChoice("braille", None, "not a terminal")
    if environ.get("NO_COLOR"):
        return GraphicsChoice("braille", None, "NO_COLOR is set")
    if setting == "auto":
        if environ.get("TERM") == "dumb":
            return GraphicsChoice("braille", None, "TERM is dumb")
        if _multiplexer(environ):
            return GraphicsChoice("braille", None, "inside tmux or screen")
    reply = parse_reply(query(), *size)
    if setting == "sixel":  # forced: the terminal's word on sixel is not asked, only its cell size
        cell = reply.cell if reply else None
        if cell is None:
            log.warning("map_renderer = sixel: no cell size reported by the terminal, assuming %dx%d", *FALLBACK_CELL)
        return GraphicsChoice("sixel", cell or FALLBACK_CELL, "map_renderer is sixel")
    if reply is None:
        return GraphicsChoice("braille", None, "no reply to the terminal probe")
    if not reply.sixel:
        return GraphicsChoice("braille", None, "terminal does not report sixel")
    if reply.cell is None:
        return GraphicsChoice("braille", None, "terminal did not report its cell size")
    return GraphicsChoice("sixel", reply.cell, "terminal reports sixel")


def _query_windows(timeout: float) -> str:
    import ctypes
    import msvcrt

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.GetStdHandle(-11)
    mode = ctypes.c_uint32()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return ""
    if not kernel32.SetConsoleMode(handle, mode.value | 0x0004):  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return ""  # a console that cannot interpret escape sequences would print the query as text
    while msvcrt.kbhit():  # type-ahead would be mistaken for an answer
        msvcrt.getwch()
    sys.stdout.write(QUERY)
    sys.stdout.flush()
    text = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if msvcrt.kbhit():
            text += msvcrt.getwch()
            if _DA1.search(text):
                break
        else:
            time.sleep(0.002)
    return text


def _query_posix(timeout: float) -> str:
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    text = ""
    try:
        tty.setcbreak(fd)
        termios.tcflush(fd, termios.TCIFLUSH)
        sys.stdout.write(QUERY)
        sys.stdout.flush()
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            if select.select([fd], [], [], left)[0]:
                text += os.read(fd, 256).decode("latin-1")
                if _DA1.search(text):
                    break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return text


def query_terminal(timeout: float = TIMEOUT) -> str:
    """Ask the terminal and return whatever came back within `timeout` seconds; never raises, never blocks longer."""
    try:
        return _query_windows(timeout) if sys.platform == "win32" else _query_posix(timeout)
    except Exception as exc:  # a probe must not stop the app from starting
        log.warning("terminal probe failed: %r", exc)
        return ""


def resolve(setting: str) -> GraphicsChoice:
    """`decide` against the real environment and terminal; logs the outcome."""
    is_tty = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        size = tuple(os.get_terminal_size())
    except OSError:
        size = (0, 0)
    choice = decide(setting, os.environ, is_tty, size, query_terminal)  # type: ignore[arg-type]
    log.info("map renderer: %s (%s%s)", choice.renderer, choice.reason, f", cell {choice.cell}" if choice.cell else "")
    return choice
