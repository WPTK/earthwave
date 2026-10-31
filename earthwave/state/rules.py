"""Alert rules: which events become a desktop toast (and bell), from the [alerts] config."""

from dataclasses import dataclass

from earthwave.config import AlertsConfig
from earthwave.models import Earthquake
from earthwave.sources import levels
from earthwave.state.events import (
    Event,
    QuakeMajor,
    QuakeNearHome,
    QuakeNew,
    QuakeUpdated,
    TsunamiChanged,
    TsunamiIssued,
)

PAGER_RANK = {"green": 0, "yellow": 1, "orange": 2, "red": 3}
BURST = 3  # this many quake toasts at once collapse into one summary


@dataclass(frozen=True, slots=True)
class Notification:
    key: str  # one toast per key
    title: str
    body: str
    urgency: str  # critical | warning | info
    bell: bool = False


def _quake_line(quake: Earthquake) -> str:
    return f"M{quake.mag:.1f} {quake.place}".strip()


class AlertRules:
    def __init__(self, alerts: AlertsConfig):
        self._config = alerts
        self._sent: set[str] = set()

    def evaluate(self, events: list[Event] | tuple[Event, ...]) -> list[Notification]:
        quakes: list[Notification] = []
        tsunamis: list[Notification] = []
        for event in events:
            if isinstance(event, QuakeMajor):
                quakes.append(self._quake("major", "Major earthquake", event.quake, "warning", bell=True))
            elif isinstance(event, QuakeNearHome):
                body = f"{_quake_line(event.quake)} ({event.distance_km:.0f} km from home)"
                quakes.append(Notification(f"near:{event.quake.id}", "Earthquake near home", body, "warning"))
            elif isinstance(event, QuakeNew) or (isinstance(event, QuakeUpdated) and "pager" in event.changed):
                if self._pager_qualifies(event.quake):
                    pager = event.quake.pager_alert
                    quakes.append(self._quake(f"pager:{pager}", f"PAGER {pager} earthquake", event.quake, "warning"))
            elif (
                isinstance(event, TsunamiIssued | TsunamiChanged)
                and self._config.tsunami
                and event.alert.level in levels.ACTIONABLE
            ):
                tsunamis.append(self._tsunami(event))
        out = [n for n in tsunamis if self._first(n)]
        fresh = [n for n in quakes if self._first(n)]
        if len(fresh) >= BURST:
            top = self._config.major_magnitude
            fresh = [
                Notification(
                    f"burst:{fresh[0].key}",
                    "Earthquakes",
                    f"{len(fresh)} notable quakes (M{top:g}+ or near you) in the last minute",
                    "warning",
                    bell=True,
                )
            ]
        return [*out, *fresh]

    def _first(self, note: Notification) -> bool:
        if note.key in self._sent:
            return False
        self._sent.add(note.key)
        return True

    def _pager_qualifies(self, quake: Earthquake) -> bool:
        floor = PAGER_RANK.get(self._config.pager_min, 1)
        return quake.pager_alert in PAGER_RANK and PAGER_RANK[quake.pager_alert] >= floor

    def _quake(self, kind: str, title: str, quake: Earthquake, urgency: str, bell: bool = False) -> Notification:
        return Notification(f"{kind}:{quake.id}", title, _quake_line(quake), urgency, bell)

    def _tsunami(self, event: TsunamiIssued | TsunamiChanged) -> Notification:
        alert = event.alert
        title = f"Tsunami {alert.level.title()}"
        if isinstance(event, TsunamiChanged):
            title += f" (was {event.previous_level.title()})"
        urgency = "critical" if alert.level in (levels.WARNING, levels.ADVISORY) else "warning"
        return Notification(f"tsunami:{alert.id}:{alert.level}", title, alert.headline, urgency, bell=True)
