"""Turn residual series into gauge-anomaly decisions against the configured thresholds. Pure; no events, no state."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from earthwave.config import AlertsConfig


class HasResidual(Protocol):
    """Either module's `Residual` (ndbc detrend or coops observed-minus-predicted)."""

    @property
    def ts(self) -> datetime: ...

    @property
    def residual_m(self) -> float: ...


@dataclass(frozen=True, slots=True)
class AnomalyDecision:
    station_id: str
    kind: str  # dart | coops
    ts: datetime  # when the largest exceedance was observed
    residual_m: float  # signed
    threshold_m: float


def threshold_m(kind: str, alerts: AlertsConfig) -> float:
    if kind == "dart":
        return alerts.gauge_anomaly_dart_m
    if kind == "coops":
        return alerts.gauge_anomaly_gauge_m
    raise ValueError(f"unknown gauge kind {kind!r}")


def find_anomaly(
    station_id: str,
    kind: str,
    residuals: list[HasResidual],
    alerts: AlertsConfig,
    since: datetime | None = None,
) -> AnomalyDecision | None:
    """The residual with the largest magnitude at or above the threshold, or None.

    Only residuals strictly after `since` (timezone-aware, like every timestamp here) count, so a caller passes
    the timestamp it last evaluated and an old pulse is not reported twice. Every sample is judged as it
    arrived; no smoothing, because a step absorbed by the detrend fit is full size on its first sample only.
    """
    limit = threshold_m(kind, alerts)
    if since is not None and since.tzinfo is None:
        raise ValueError("since must be timezone-aware (UTC)")
    # NaN compares false against every threshold, so a NaN residual is never reported
    over = [r for r in residuals if (since is None or r.ts > since) and abs(r.residual_m) >= limit]
    if not over:
        return None
    worst = max(over, key=lambda r: abs(r.residual_m))
    return AnomalyDecision(station_id, kind, worst.ts, worst.residual_m, limit)
