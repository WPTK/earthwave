"""winterm: drive and read a real Windows terminal window (Windows Terminal, conhost, VS Code) with no app code.

Pieces, each usable alone:

* ``Win``: window enumeration, focus, move/resize, screenshots (``mss`` of the screen rectangle and ``PrintWindow``).
* ``Input``: ``SendInput`` keyboard (virtual keys, so the terminal sees real key events) and mouse (absolute moves,
  motion first, press, release), plus the ``GetLastInputInfo`` idle check used before touching a shared desktop.
* ``UiaText``: read the screen text of a terminal window through the UI Automation ``TextPattern`` and find the pixel
  rectangle of a piece of text (``FindText`` + ``GetBoundingRectangles``), which gives the cell to pixel math.
* ``clipboard_text`` / ``set_clipboard_text``: the read-back used for VS Code (select all, copy).
* ``process_tree`` / ``kill_tree``: teardown by PID, never by image name.

Windows only. Importing this module on another OS fails on purpose.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes as wt
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Physical pixels everywhere: UIA rectangles, GetWindowRect and SendInput must share one coordinate space.
with contextlib.suppress(Exception):
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2

ULONG_PTR = ctypes.c_size_t

VK_SHIFT, VK_CONTROL, VK_MENU, VK_RETURN, VK_ESCAPE = 0x10, 0x11, 0x12, 0x0D, 0x1B
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_SCANCODE = 0x0002, 0x0004, 0x0008
MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x0002, 0x0004, 0x8000
SW_RESTORE, SW_MAXIMIZE, SW_SHOWNORMAL = 9, 3, 1


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _InputUnion)]


user32.SendInput.argtypes = (wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wt.UINT
user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = (wt.HWND,)
user32.ShowWindow.argtypes = (wt.HWND, ctypes.c_int)
user32.IsWindow.argtypes = (wt.HWND,)
user32.IsWindowVisible.argtypes = (wt.HWND,)
user32.GetWindowRect.argtypes = (wt.HWND, ctypes.POINTER(wt.RECT))
user32.GetClientRect.argtypes = (wt.HWND, ctypes.POINTER(wt.RECT))
user32.MoveWindow.argtypes = (wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.BOOL)
user32.GetWindowTextW.argtypes = (wt.HWND, wt.LPWSTR, ctypes.c_int)
user32.GetClassNameW.argtypes = (wt.HWND, wt.LPWSTR, ctypes.c_int)
user32.GetWindowThreadProcessId.argtypes = (wt.HWND, ctypes.POINTER(wt.DWORD))
user32.GetWindowDC.argtypes = (wt.HWND,)
user32.GetWindowDC.restype = wt.HDC
user32.ReleaseDC.argtypes = (wt.HWND, wt.HDC)
user32.PrintWindow.argtypes = (wt.HWND, wt.HDC, wt.UINT)
user32.PostMessageW.argtypes = (wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
user32.VkKeyScanW.argtypes = (wt.WCHAR,)
user32.VkKeyScanW.restype = ctypes.c_short
user32.MapVirtualKeyW.argtypes = (wt.UINT, wt.UINT)
gdi32.CreateCompatibleDC.argtypes = (wt.HDC,)
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleBitmap.argtypes = (wt.HDC, ctypes.c_int, ctypes.c_int)
gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = (wt.HDC, wt.HGDIOBJ)
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = (wt.HGDIOBJ,)
gdi32.DeleteDC.argtypes = (wt.HDC,)


class Skip(Exception):
    """A step that cannot run here (not a failure of the terminal)."""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------------------------------------------------
# windows


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    cls: str
    title: str
    pid: int
    visible: bool
    rect: tuple[int, int, int, int]  # left, top, right, bottom

    def __str__(self) -> str:
        return f"hwnd={self.hwnd:#x} class={self.cls!r} pid={self.pid} visible={self.visible} rect={self.rect} title={self.title!r}"


def _text(fn: Callable[..., int], hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    fn(hwnd, buf, 512)
    return buf.value


def window_info(hwnd: int) -> WindowInfo:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return WindowInfo(
        hwnd,
        _text(user32.GetClassNameW, hwnd),
        _text(user32.GetWindowTextW, hwnd),
        pid.value,
        bool(user32.IsWindowVisible(hwnd)),
        (r.left, r.top, r.right, r.bottom),
    )


def all_windows() -> list[WindowInfo]:
    found: list[WindowInfo] = []
    proto = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd: int, _lparam: int) -> bool:
        found.append(window_info(hwnd))
        return True

    user32.EnumWindows(proto(cb), 0)
    return found


def is_window(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd))


def screen_size() -> tuple[int, int]:
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


def foreground() -> int:
    return user32.GetForegroundWindow() or 0


def focus(hwnd: int, tries: int = 5) -> bool:
    """Bring a window to the foreground. The ALT tap is the classic way past the foreground lock."""
    if foreground() == hwnd:  # no ALT tap then: in VS Code it would move focus to the menu bar and eat the next key
        return True
    user32.ShowWindow(hwnd, SW_RESTORE)
    for _ in range(tries):
        key_event(VK_MENU, False)
        key_event(VK_MENU, True)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.3)
        if foreground() == hwnd:
            return True
    return foreground() == hwnd


def move_resize(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    user32.ShowWindow(hwnd, SW_SHOWNORMAL)
    user32.MoveWindow(hwnd, x, y, w, h, True)


def maximize(hwnd: int) -> None:
    user32.ShowWindow(hwnd, SW_MAXIMIZE)


def close_window(hwnd: int) -> None:
    user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE


def wait_for(pred: Callable[[], Any], timeout: float, what: str, interval: float = 0.25) -> Any:
    """Poll ``pred`` until it returns something truthy; raise TimeoutError naming ``what``."""
    end = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < end:
        try:
            last = pred()
        except Exception as exc:  # a UIA read can fail while a window is being built
            last = None
            log(f"  (wait {what}: {type(exc).__name__}: {str(exc)[:100]})")
        if last:
            return last
        time.sleep(interval)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}")


# ---------------------------------------------------------------------------------------------------------------------
# screenshots


def screenshot_screen(path: Path, bbox: tuple[int, int, int, int] | None = None) -> bool:
    """PNG of the screen (or of ``bbox`` = left, top, right, bottom). What the user would see."""
    import mss
    import mss.tools

    with mss.mss() as sct:
        mon: dict[str, int] = (
            {"left": bbox[0], "top": bbox[1], "width": bbox[2] - bbox[0], "height": bbox[3] - bbox[1]}
            if bbox
            else dict(sct.monitors[0])
        )
        shot = sct.grab(mon)
        mss.tools.to_png(shot.rgb, shot.size, output=str(path))
    return path.exists() and path.stat().st_size > 0


def screenshot_printwindow(hwnd: int, path: Path) -> tuple[bool, float]:
    """PNG through ``PrintWindow(PW_RENDERFULLCONTENT)``; returns (ok, mean brightness 0..255) to spot a black result."""
    from PIL import Image, ImageStat

    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return False, 0.0
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    ok = bool(user32.PrintWindow(hwnd, mdc, 2))

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wt.DWORD),
            ("biWidth", wt.LONG),
            ("biHeight", wt.LONG),
            ("biPlanes", wt.WORD),
            ("biBitCount", wt.WORD),
            ("biCompression", wt.DWORD),
            ("biSizeImage", wt.DWORD),
            ("biXPelsPerMeter", wt.LONG),
            ("biYPelsPerMeter", wt.LONG),
            ("biClrUsed", wt.DWORD),
            ("biClrImportant", wt.DWORD),
        ]

    bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits.argtypes = (wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT)
    lines = gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.SelectObject(mdc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(hwnd, hdc)
    if not lines:
        return False, 0.0
    img = Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "BGRA", 0, 1).convert("RGB")
    img.save(path)
    return ok, float(ImageStat.Stat(img.convert("L")).mean[0])


# ---------------------------------------------------------------------------------------------------------------------
# input


def _send(*inputs: INPUT) -> int:
    arr = (INPUT * len(inputs))(*inputs)
    return int(user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)))


def key_event(vk: int, up: bool) -> int:
    scan = user32.MapVirtualKeyW(vk, 0)
    flags = KEYEVENTF_KEYUP if up else 0
    return _send(INPUT(1, _InputUnion(ki=KEYBDINPUT(vk, scan, flags, 0, 0))))


def unicode_event(ch: str, up: bool) -> int:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return _send(INPUT(1, _InputUnion(ki=KEYBDINPUT(0, ord(ch), flags, 0, 0))))


def tap(vk: int, hold: float = 0.03) -> None:
    key_event(vk, False)
    time.sleep(hold)
    key_event(vk, True)


def chord(*vks: int, hold: float = 0.05) -> None:
    """Press the keys in order, release in reverse: ``chord(VK_CONTROL, VK_MENU, ord('A'))``."""
    for vk in vks:
        key_event(vk, False)
        time.sleep(0.02)
    time.sleep(hold)
    for vk in reversed(vks):
        key_event(vk, True)
        time.sleep(0.02)


def send_text(text: str, delay: float = 0.04) -> int:
    """Type text as real key presses (virtual keys, with Shift when the layout needs it). Returns events inserted."""
    sent = 0
    for ch in text:
        if ch == "\n":
            tap(VK_RETURN)
            sent += 2
            continue
        code = user32.VkKeyScanW(ch)
        if code == -1:  # not on this layout: fall back to a Unicode packet
            sent += unicode_event(ch, False) + unicode_event(ch, True)
        else:
            vk, state = code & 0xFF, (code >> 8) & 0xFF
            mods = (
                ([VK_SHIFT] if state & 1 else [])
                + ([VK_CONTROL] if state & 2 else [])
                + ([VK_MENU] if state & 4 else [])
            )
            for m in mods:
                sent += key_event(m, False)
            sent += key_event(vk, False)
            time.sleep(0.01)
            sent += key_event(vk, True)
            for m in reversed(mods):
                sent += key_event(m, True)
        time.sleep(delay)
    return sent


def _abs(x: int, y: int) -> tuple[int, int]:
    w, h = screen_size()
    return round(x * 65535 / max(w - 1, 1)), round(y * 65535 / max(h - 1, 1))


def mouse_move(x: int, y: int) -> int:
    ax, ay = _abs(x, y)
    return _send(INPUT(0, _InputUnion(mi=MOUSEINPUT(ax, ay, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, 0, 0))))


_BUTTON_FLAGS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
MOUSEEVENTF_WHEEL = 0x0800


def mouse_button(down: bool, button: str = "left") -> int:
    flag = _BUTTON_FLAGS[button][0 if down else 1]
    return _send(INPUT(0, _InputUnion(mi=MOUSEINPUT(0, 0, 0, flag, 0, 0))))


def _approach(x: int, y: int, steps: int) -> int:
    sent = 0
    for i in range(steps, 0, -1):
        sent += mouse_move(x - 3 * i, y - i)
        time.sleep(0.03)
    sent += mouse_move(x, y)
    time.sleep(0.12)
    return sent


def click_at(x: int, y: int, *, approach: int = 6, button: str = "left") -> int:
    """A human-shaped click: approach in small moves (a terminal reports motion first), press, hold, release."""
    sent = _approach(x, y, approach)
    sent += mouse_button(True, button)
    time.sleep(0.07)
    sent += mouse_button(False, button)
    time.sleep(0.15)
    sent += mouse_move(x + 2, y)  # motion after the release, as a real hand produces
    return sent


def double_click_at(x: int, y: int, *, approach: int = 6) -> int:
    """Two presses at one spot well inside the system double-click time (500 ms by default)."""
    sent = _approach(x, y, approach)
    for _ in range(2):
        sent += mouse_button(True)
        time.sleep(0.04)
        sent += mouse_button(False)
        time.sleep(0.08)
    time.sleep(0.15)
    return sent


def wheel_at(x: int, y: int, notches: int, *, approach: int = 4) -> int:
    """Turn the wheel ``notches`` detents (negative = towards the user = scroll down) with the pointer at x, y."""
    sent = _approach(x, y, approach)
    step = -120 if notches < 0 else 120
    for _ in range(abs(notches)):
        data = step & 0xFFFFFFFF
        sent += _send(INPUT(0, _InputUnion(mi=MOUSEINPUT(0, 0, data, MOUSEEVENTF_WHEEL, 0, 0))))
        time.sleep(0.08)
    time.sleep(0.2)
    return sent


def drag_between(x1: int, y1: int, x2: int, y2: int, *, steps: int = 14, button: str = "left") -> int:
    """Press at (x1, y1), move to (x2, y2) in small steps, release."""
    sent = _approach(x1, y1, 3)
    sent += mouse_button(True, button)
    time.sleep(0.1)
    for i in range(1, steps + 1):
        sent += mouse_move(x1 + (x2 - x1) * i // steps, y1 + (y2 - y1) * i // steps)
        time.sleep(0.03)
    time.sleep(0.1)
    sent += mouse_button(False, button)
    time.sleep(0.2)
    return sent


def hover_at(x: int, y: int, dwell: float = 1.0) -> int:
    sent = _approach(x, y, 4)
    sent += mouse_move(x + 1, y)
    time.sleep(dwell)
    return sent


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def idle_seconds() -> float:
    """Seconds since the last real keyboard or mouse input on this desktop (GetLastInputInfo)."""
    info = _LASTINPUTINFO(ctypes.sizeof(_LASTINPUTINFO), 0)
    user32.GetLastInputInfo(ctypes.byref(info))
    kernel32.GetTickCount.restype = wt.DWORD
    return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


# ---------------------------------------------------------------------------------------------------------------------
# clipboard


def clipboard_text() -> str:
    user32.OpenClipboard.argtypes = (wt.HWND,)
    user32.GetClipboardData.restype = wt.HANDLE
    user32.GetClipboardData.argtypes = (wt.UINT,)
    kernel32.GlobalLock.argtypes = (wt.HANDLE,)
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = (wt.HANDLE,)
    for _ in range(20):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return ""
    try:
        h = user32.GetClipboardData(13)  # CF_UNICODETEXT
        if not h:
            return ""
        p = kernel32.GlobalLock(h)
        try:
            return ctypes.wstring_at(p) if p else ""
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def clear_clipboard() -> None:
    for _ in range(20):
        if user32.OpenClipboard(None):
            user32.EmptyClipboard()
            user32.CloseClipboard()
            return
        time.sleep(0.05)


# ---------------------------------------------------------------------------------------------------------------------
# UI Automation text


def _norm(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))


class UiaText:
    """The screen text of one terminal window, read through its UIA TextPattern."""

    def __init__(self, hwnd: int, *, max_depth: int = 8) -> None:
        import uiautomation as auto

        self.auto = auto
        self.hwnd = hwnd
        self.max_depth = max_depth
        self._ctrl: Any = None
        self._pattern: Any = None

    def _locate(self) -> tuple[Any, Any]:
        """The element with a TextPattern that holds the most text (the terminal screen, not a title or a tab)."""
        auto = self.auto
        root = auto.ControlFromHandle(self.hwnd)
        queue = [(root, 0)]
        best: tuple[int, Any, Any] | None = None
        self.candidates: list[str] = []
        while queue:
            ctrl, depth = queue.pop(0)
            pattern = None
            with contextlib.suppress(Exception):
                pattern = ctrl.GetPattern(auto.PatternId.TextPattern)
            if pattern is not None:
                size = -1
                with contextlib.suppress(Exception):
                    size = len(pattern.DocumentRange.GetText(-1))
                self.candidates.append(
                    f"{ctrl.ControlTypeName} class={ctrl.ClassName!r} name={ctrl.Name[:30]!r} chars={size}"
                )
                if best is None or size > best[0]:
                    best = (size, ctrl, pattern)
            if depth < self.max_depth:
                with contextlib.suppress(Exception):
                    queue.extend((child, depth + 1) for child in ctrl.GetChildren())
        if best is None:
            raise LookupError(f"no UIA element with a TextPattern under hwnd {self.hwnd:#x}")
        return best[1], best[2]

    @property
    def control(self) -> Any:
        if self._ctrl is None:
            self._ctrl, self._pattern = self._locate()
        return self._ctrl

    @property
    def pattern(self) -> Any:
        _ = self.control
        return self._pattern

    def read(self) -> str:
        """All text of the document range, trailing blanks trimmed per line."""
        try:
            return _norm(self.pattern.DocumentRange.GetText(-1))
        except Exception:
            self._ctrl = self._pattern = None  # the element may have been rebuilt: look again next time
            raise

    def find_rect(self, text: str) -> tuple[int, int, int, int] | None:
        """Pixel rectangle (left, top, right, bottom) of the first occurrence of ``text``, or None."""
        rng = self.pattern.DocumentRange.FindText(text, False, False)
        if rng is None:
            return None
        rects = rng.GetBoundingRectangles()
        if not rects:
            return None
        r = rects[0]
        return int(r.left), int(r.top), int(r.right), int(r.bottom)

    def control_rect(self) -> tuple[int, int, int, int]:
        r = self.control.BoundingRectangle
        return int(r.left), int(r.top), int(r.right), int(r.bottom)

    def describe(self) -> str:
        c = self.control
        return f"{c.ControlTypeName} class={c.ClassName!r} name={c.Name[:40]!r} rect={self.control_rect()}"


def dump_uia_tree(hwnd: int, path: Path, max_depth: int = 6, max_nodes: int = 400) -> int:
    """Write the UIA tree under a window to a text file (for working out what a terminal exposes)."""
    import uiautomation as auto

    lines: list[str] = []
    count = 0

    def walk(ctrl: Any, depth: int) -> None:
        nonlocal count
        if count >= max_nodes:
            return
        count += 1
        has_text = False
        with contextlib.suppress(Exception):
            has_text = ctrl.GetPattern(auto.PatternId.TextPattern) is not None
        r = ctrl.BoundingRectangle
        lines.append(
            f"{'  ' * depth}{ctrl.ControlTypeName} class={ctrl.ClassName!r} name={ctrl.Name[:50]!r} "
            f"rect=({r.left},{r.top},{r.right},{r.bottom}) text_pattern={has_text}"
        )
        if depth < max_depth:
            for child in ctrl.GetChildren():
                walk(child, depth + 1)

    with contextlib.suppress(Exception):
        walk(auto.ControlFromHandle(hwnd), 0)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return count


# ---------------------------------------------------------------------------------------------------------------------
# processes


def process_tree(pid: int) -> list[psutil.Process]:
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return []
    return [root, *root.children(recursive=True)]


def kill_tree(pid: int) -> list[int]:
    """Kill a process and its descendants by PID; returns the PIDs that were alive."""
    procs = process_tree(pid)
    killed: list[int] = []
    for p in reversed(procs):
        with contextlib.suppress(psutil.Error):
            p.kill()
            killed.append(p.pid)
    psutil.wait_procs(procs, timeout=5)
    return killed


def survivors(pids: set[int], image_dirs: tuple[str, ...] = ()) -> list[str]:
    """PIDs from ``pids`` still alive, plus any process whose executable lives under one of ``image_dirs``."""
    out: list[str] = []
    dirs = tuple(d.lower() for d in image_dirs)
    for p in psutil.process_iter(["pid", "name", "exe"]):
        try:
            exe = (p.info["exe"] or "").lower()
        except psutil.Error:
            continue
        if p.info["pid"] in pids or (dirs and exe.startswith(dirs)):
            out.append(f"{p.info['pid']}:{p.info['name']}")
    return out


def lines_matching(text: str, pattern: str) -> list[str]:
    return [line for line in text.split("\n") if re.search(pattern, line)]


advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
kernel32.CloseHandle.argtypes = (wt.HANDLE,)
advapi32.OpenProcessToken.argtypes = (wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE))
advapi32.GetTokenInformation.argtypes = (wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD))
advapi32.GetSidSubAuthorityCount.argtypes = (ctypes.c_void_p,)
advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
advapi32.GetSidSubAuthority.argtypes = (ctypes.c_void_p, wt.DWORD)
advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wt.DWORD)


def integrity_of(pid: int) -> str:
    """Mandatory integrity level of a process token: Low / Medium / High / System, or an error string."""
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return f"open failed ({ctypes.get_last_error()})"
    tok = wt.HANDLE()
    try:
        if not advapi32.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):  # TOKEN_QUERY
            return f"token failed ({ctypes.get_last_error()})"
        need = wt.DWORD()
        advapi32.GetTokenInformation(tok, 25, None, 0, ctypes.byref(need))  # TokenIntegrityLevel
        buf = ctypes.create_string_buffer(need.value)
        if not advapi32.GetTokenInformation(tok, 25, buf, need, ctypes.byref(need)):
            return f"info failed ({ctypes.get_last_error()})"
        sid = ctypes.c_void_p.from_buffer(buf).value
        count = advapi32.GetSidSubAuthorityCount(sid).contents.value
        rid = advapi32.GetSidSubAuthority(sid, count - 1).contents.value
        names = {0x1000: "Low", 0x2000: "Medium", 0x2100: "MediumPlus", 0x3000: "High", 0x4000: "System"}
        return f"{names.get(rid, 'unknown')} ({rid:#x})"
    finally:
        if tok:
            kernel32.CloseHandle(tok)
        kernel32.CloseHandle(h)


# ---------------------------------------------------------------------------------------------------------------------
# pixels: prove what the terminal really looks like


def _lum(rgb: tuple[int, int, int]) -> float:
    def ch(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def analyze_png(path: Path, *, margin: float = 0.1) -> dict[str, Any]:
    """Background and text colours of a screenshot, from pixels.

    Background = the most common colour in the central area (frame and scroll bars cropped). Text = the colour with the
    highest contrast against it among colours that cover at least 30 pixels. Returns the WCAG contrast ratio of the two
    and the background's relative luminance (0 black .. 1 white).
    """
    from collections import Counter

    from PIL import Image

    img = Image.open(path).convert("RGB")
    wd, ht = img.size
    box = (int(wd * margin), int(ht * margin), int(wd * (1 - margin)), int(ht * (1 - margin)))
    counts = Counter(img.crop(box).getdata())
    bg = counts.most_common(1)[0][0]
    fg, best = bg, 1.0
    for colour, n in counts.items():
        if n >= 30:
            c = contrast(bg, colour)
            if c > best:
                fg, best = colour, c
    return {
        "bg": "#{:02x}{:02x}{:02x}".format(*bg),
        "bg_share": round(counts[bg] / sum(counts.values()), 3),
        "bg_luminance": round(_lum(bg), 3),
        "text": "#{:02x}{:02x}{:02x}".format(*fg),
        "contrast": round(best, 2),
        "distinct_colours": len(counts),
    }


def dismiss_blockers(own_hwnd: int) -> list[str]:
    """Get a system flyout or full-screen prompt out of the way of the terminal (seen on the arm64 image).

    Looks at the foreground window and at full-screen top-level windows of class ``Windows.UI.Core.CoreWindow`` (a sign-in
    prompt, the Search flyout). Escape first, then WM_CLOSE, then the owning process is killed by PID. Returns what was
    done, for the log.
    """
    done: list[str] = []
    sw, sh = screen_size()
    for _ in range(3):
        fg = window_info(foreground()) if foreground() else None
        wins = [fg] if fg else []
        wins += [
            x
            for x in all_windows()
            if x.visible and x.rect[2] - x.rect[0] >= sw - 4 and x.rect[3] - x.rect[1] >= sh - 4
        ]
        acted = False
        for win in wins:
            if win.hwnd == own_hwnd or not win.visible or win.cls != "Windows.UI.Core.CoreWindow":
                continue
            try:
                name = psutil.Process(win.pid).name()
            except psutil.Error:
                name = "?"
            tap(VK_ESCAPE)
            time.sleep(0.6)
            if not (is_window(win.hwnd) and window_info(win.hwnd).visible):
                done.append(f"escaped {win.title!r} owner {name} pid {win.pid}")
                acted = True
                break
            close_window(win.hwnd)
            time.sleep(1.0)
            if is_window(win.hwnd) and window_info(win.hwnd).visible:
                with contextlib.suppress(psutil.Error):
                    psutil.Process(win.pid).kill()
                time.sleep(1.0)
                done.append(f"killed {win.title!r} owner {name} pid {win.pid}; still there: {is_window(win.hwnd)}")
            else:
                done.append(f"closed {win.title!r} owner {name} pid {win.pid}")
            acted = True
            break
        if not acted:
            break
    return done


# ---------------------------------------------------------------------------------------------------------------------
# display scale (DPI) of the primary source, through the display-config API


def _dpi_sources() -> list[tuple[int, int, int]]:
    import struct

    np_, nm = wt.UINT(), wt.UINT()
    user32.GetDisplayConfigBufferSizes.argtypes = (wt.UINT, ctypes.POINTER(wt.UINT), ctypes.POINTER(wt.UINT))
    rc = user32.GetDisplayConfigBufferSizes(2, ctypes.byref(np_), ctypes.byref(nm))  # QDC_ONLY_ACTIVE_PATHS
    if rc:
        raise OSError(f"GetDisplayConfigBufferSizes returned {rc}")
    paths = ctypes.create_string_buffer(72 * max(np_.value, 1))
    modes = ctypes.create_string_buffer(64 * max(nm.value, 1))
    user32.QueryDisplayConfig.argtypes = (
        wt.UINT,
        ctypes.POINTER(wt.UINT),
        ctypes.c_void_p,
        ctypes.POINTER(wt.UINT),
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    rc = user32.QueryDisplayConfig(2, ctypes.byref(np_), paths, ctypes.byref(nm), modes, None)
    if rc:
        raise OSError(f"QueryDisplayConfig returned {rc}")
    return [struct.unpack_from("<IiI", paths.raw, i * 72) for i in range(np_.value)]


def get_dpi_scale() -> tuple[int, int, int]:
    """(min, current, max) relative to the recommended scale (0 = recommended; +1 is the next step up, 125% -> 150%...)."""
    import struct

    lo, hi, sid = _dpi_sources()[0]
    buf = ctypes.create_string_buffer(struct.pack("<iIIiI", -3, 32, lo, hi, sid) + bytes(12))
    user32.DisplayConfigGetDeviceInfo.argtypes = (ctypes.c_void_p,)
    rc = user32.DisplayConfigGetDeviceInfo(buf)
    if rc:
        raise OSError(f"DisplayConfigGetDeviceInfo(GET_DPI_SCALE) returned {rc}")
    return struct.unpack_from("<iii", buf.raw, 20)


def set_dpi_scale(rel: int) -> None:
    import struct

    lo, hi, sid = _dpi_sources()[0]
    buf = ctypes.create_string_buffer(struct.pack("<iIIiIi", -4, 24, lo, hi, sid, rel))
    user32.DisplayConfigSetDeviceInfo.argtypes = (ctypes.c_void_p,)
    rc = user32.DisplayConfigSetDeviceInfo(buf)
    if rc:
        raise OSError(f"DisplayConfigSetDeviceInfo(SET_DPI_SCALE {rel}) returned {rc}")
