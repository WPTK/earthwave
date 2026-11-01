"""Alert rules: which events become a desktop toast (and bell), from the [alerts] config."""

from dataclasses import dataclass, replace
from datetime import datetime

from earthwave.config import AlertsConfig
from earthwave.models import Earthquake
from earthwave.sources import levels
from earthwave.state.events import (
    Event,
    GaugeAnomaly,
    QuakeMajor,
    QuakeNearHome,
    QuakeNew,
    QuakeUpdated,
    TsunamiChanged,
    TsunamiIssued,
)
from earthwave.ui.labels import fmt_age, fmt_time

PAGER_RANK = {"green": 0, "yellow": 1, "orange": 2, "red": 3}
BURST = 3  # this many quake toasts at once collapse into one summary
OLD_QUAKE = 3600.0  # seconds: a quake announced this long after it happened (an outage, a sleep) says how old it is
SENT_LIMIT = 500  # toast keys remembered; the oldest are forgotten first


@dataclass(frozen=True, slots=True)
class Notification:
    key: str  # one toast per key
    title: str
    body: str
    urgency: str  # critical | warning | info
    bell: bool = False


def quake_line(quake: Earthquake, now: datetime | None = None) -> str:
    """'M6.1 place', with 'N ago' when the quake is old news by the time it is announced."""
    line = f"M{quake.mag:.1f} {quake.place}".strip()
    age = (now - quake.time).total_seconds() if now is not None else 0.0
    return f"{line}, {fmt_age(age)} ago" if age >= OLD_QUAKE else line


class AlertRules:
    def __init__(self, alerts: AlertsConfig):
        self._config = alerts
        self._sent: dict[str, None] = {}

    def evaluate(self, events: list[Event] | tuple[Event, ...], now: datetime | None = None) -> list[Notification]:
        quakes: list[tuple[int, Earthquake, Notification]] = []  # (urgency rank, quake, note); 0 is most urgent
        tsunamis: list[Notification] = []
        home_km: dict[str, float] = {}
        for event in events:
            if isinstance(event, QuakeMajor):
                quakes.append(
                    (0, event.quake, self._quake("major", "Major earthquake", event.quake, "warning", now, bell=True))
                )
            elif isinstance(event, QuakeNearHome):
                home_km[event.quake.id] = event.distance_km
                body = f"{quake_line(event.quake, now)} ({event.distance_km:.0f} km from home)"
                quakes.append(
                    (1, event.quake, Notification(f"near:{event.quake.id}", "Earthquake near home", body, "warning"))
                )
            elif isinstance(event, QuakeNew) or (isinstance(event, QuakeUpdated) and "pager" in event.changed):
                if self._pager_qualifies(event.quake):
                    pager = event.quake.pager_alert
                    quakes.append(
                        (
                            2,
                            event.quake,
                            self._quake(f"pager:{pager}", f"PAGER {pager} earthquake", event.quake, "warning", now),
                        )
                    )
            elif (
                isinstance(event, TsunamiIssued | TsunamiChanged)
                and self._config.tsunami
                and event.alert.level in levels.ACTIONABLE
            ):
                tsunamis.append(self._tsunami(event))
            elif isinstance(event, GaugeAnomaly) and self._config.tsunami:
                tsunamis.append(self._gauge(event))
        out = [n for n in tsunamis if self._first(n)]
        # One quake can raise several notifications (major, near home, PAGER); it still counts, and toasts, once.
        per_quake: dict[str, tuple[Earthquake, Notification]] = {}
        for _, quake, note in sorted(quakes, key=lambda item: item[0]):
            if self._first(note) and quake.id not in per_quake:
                if note.key.startswith("major:") and quake.id in home_km:  # the near-home fact must not be lost
                    note = replace(note, body=f"{note.body} ({home_km[quake.id]:.0f} km from home)")
                per_quake[quake.id] = (quake, note)
        fresh = [note for _, note in per_quake.values()]
        if len(per_quake) >= BURST:
            top = max((q for q, _ in per_quake.values()), key=lambda q: q.mag)
            fresh = [
                Notification(
                    f"burst:{fresh[0].key}",
                    "Earthquakes",
                    f"{len(fresh)} notable quakes, largest {quake_line(top, now)}",
                    "warning",
                    bell=True,
                )
            ]
        return [*out, *fresh]

    def _first(self, note: Notification) -> bool:
        if note.key in self._sent:
            return False
        self._sent[note.key] = None
        while len(self._sent) > SENT_LIMIT:
            del self._sent[next(iter(self._sent))]
        return True

    def _pager_qualifies(self, quake: Earthquake) -> bool:
        floor = PAGER_RANK.get(self._config.pager_min, 1)
        return quake.pager_alert in PAGER_RANK and PAGER_RANK[quake.pager_alert] >= floor

    def _quake(
        self, kind: str, title: str, quake: Earthquake, urgency: str, now: datetime | None, bell: bool = False
    ) -> Notification:
        return Notification(f"{kind}:{quake.id}", title, quake_line(quake, now), urgency, bell)

    def _tsunami(self, event: TsunamiIssued | TsunamiChanged) -> Notification:
        alert = event.alert
        title = f"Tsunami {alert.level.title()}"
        bell = True
        if isinstance(event, TsunamiChanged):
            if event.previous_level == alert.level:
                title += " (update)"  # a follow-up at the same level: toast, but no repeated bell
                bell = False
            else:
                title += f" (was {event.previous_level.title()})"
        urgency = "critical" if alert.level in (levels.WARNING, levels.ADVISORY) else "warning"
        return Notification(f"tsunami:{alert.id}:{alert.level}", title, alert.headline, urgency, bell=bell)

    def _gauge(self, event: GaugeAnomaly) -> Notification:
        """One toast per anomaly episode: the store only raises the event when an episode starts."""
        name = event.name or event.station_id
        body = f"{name}: {event.residual_m:+.3f} m from the tide (threshold {event.threshold_m:g} m)"
        if event.late:
            body += f" at {fmt_time(event.ts)}"
        return Notification(f"gauge:{event.station_id}:{event.ts:%Y%m%dT%H%M%S}", "Gauge anomaly", body, "warning")
