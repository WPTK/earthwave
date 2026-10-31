"""Desktop notifications behind a small protocol: Windows toast, notify-send, osascript, and a test fake."""

import asyncio
import logging
import shutil
import subprocess
import sys
from typing import Protocol

from earthwave.state.rules import Notification

log = logging.getLogger(__name__)


class Notifier(Protocol):
    async def send(self, note: Notification) -> None:
        """Show one notification. Implementations swallow and log their own failures."""
        ...


class RecordingNotifier:
    """Collects notifications instead of showing them (tests, and a stand-in under replay)."""

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send(self, note: Notification) -> None:
        self.sent.append(note)


class _ThreadedNotifier:
    """Runs a blocking call off the event loop; any failure is logged, never raised."""

    async def send(self, note: Notification) -> None:
        try:
            await asyncio.to_thread(self._show, note)
        except Exception as exc:
            log.warning("desktop notification failed: %r", exc)

    def _show(self, note: Notification) -> None:
        raise NotImplementedError


class WindowsToast(_ThreadedNotifier):
    """winotify spawns a hidden PowerShell per toast, so it is slow and may be blocked by policy."""

    def _show(self, note: Notification) -> None:
        from winotify import Notification as Toast  # win32 only

        toast = Toast(app_id="earthwave", title=note.title, msg=note.body, duration="long")
        toast.show()


class NotifySend(_ThreadedNotifier):
    def _show(self, note: Notification) -> None:
        level = "critical" if note.urgency == "critical" else "normal"
        subprocess.run(["notify-send", "-u", level, "-a", "earthwave", note.title, note.body], check=False, timeout=10)


class Osascript(_ThreadedNotifier):
    def _show(self, note: Notification) -> None:
        def quote(text: str) -> str:
            return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

        script = f"display notification {quote(note.body)} with title {quote(note.title)}"
        subprocess.run(["osascript", "-e", script], check=False, timeout=10)


def default_notifier(platform: str | None = None) -> Notifier | None:
    """The platform's notifier, or None when the platform has no usable one."""
    platform = platform or sys.platform
    if platform == "win32":
        return WindowsToast()
    if platform == "darwin":
        return Osascript()
    return NotifySend() if shutil.which("notify-send") else None
