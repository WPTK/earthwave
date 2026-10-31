"""Events the store and scheduler emit when something changes. Frozen, with a `kind` string and a log-friendly str()."""

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from earthwave.models import Earthquake, TsunamiAlert


@dataclass(frozen=True, slots=True)
class QuakeNew:
    quake: Earthquake
    kind: ClassVar[str] = "quake_new"

    def __str__(self) -> str:
        return f"new M{self.quake.mag:.1f} {self.quake.place} ({self.quake.id})"


@dataclass(frozen=True, slots=True)
class QuakeUpdated:
    quake: Earthquake
    changed: tuple[str, ...]
    kind: ClassVar[str] = "quake_updated"

    def __str__(self) -> str:
        return f"updated {self.quake.id}: {', '.join(self.changed)}"


@dataclass(frozen=True, slots=True)
class QuakeMajor:
    quake: Earthquake
    kind: ClassVar[str] = "quake_major"

    def __str__(self) -> str:
        return f"major M{self.quake.mag:.1f} {self.quake.place} ({self.quake.id})"


@dataclass(frozen=True, slots=True)
class QuakeNearHome:
    quake: Earthquake
    distance_km: float
    kind: ClassVar[str] = "quake_near_home"

    def __str__(self) -> str:
        return f"near home M{self.quake.mag:.1f} {self.quake.place}, {self.distance_km:.0f} km ({self.quake.id})"


@dataclass(frozen=True, slots=True)
class TsunamiIssued:
    alert: TsunamiAlert
    kind: ClassVar[str] = "tsunami_issued"

    def __str__(self) -> str:
        return f"tsunami {self.alert.level} issued: {self.alert.headline}"


@dataclass(frozen=True, slots=True)
class TsunamiChanged:
    alert: TsunamiAlert
    previous_level: str
    kind: ClassVar[str] = "tsunami_changed"

    def __str__(self) -> str:
        return f"tsunami {self.alert.id} {self.previous_level} -> {self.alert.level}"


@dataclass(frozen=True, slots=True)
class TsunamiCancelled:
    alert: TsunamiAlert
    kind: ClassVar[str] = "tsunami_cancelled"

    def __str__(self) -> str:
        return f"tsunami {self.alert.id} cancelled"


@dataclass(frozen=True, slots=True)
class GaugeAnomaly:
    station_id: str
    residual_m: float
    ts: datetime
    name: str = ""
    threshold_m: float = 0.0
    late: bool = False  # seen more than GAUGE_FRESH after it happened; messages then say when
    kind: ClassVar[str] = "gauge_anomaly"

    def __str__(self) -> str:
        return f"gauge anomaly {self.name or self.station_id}: {self.residual_m:+.3f} m"


@dataclass(frozen=True, slots=True)
class SourceDegraded:
    source: str
    error: str
    kind: ClassVar[str] = "source_degraded"

    def __str__(self) -> str:
        return f"{self.source} degraded: {self.error}"


@dataclass(frozen=True, slots=True)
class SourceRecovered:
    source: str
    kind: ClassVar[str] = "source_recovered"

    def __str__(self) -> str:
        return f"{self.source} recovered"


Event = (
    QuakeNew
    | QuakeUpdated
    | QuakeMajor
    | QuakeNearHome
    | TsunamiIssued
    | TsunamiChanged
    | TsunamiCancelled
    | GaugeAnomaly
    | SourceDegraded
    | SourceRecovered
)
