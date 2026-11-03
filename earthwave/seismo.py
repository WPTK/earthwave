"""Seismology arithmetic: radiated energy and P/S travel times. Pure; no Textual, no network."""

import math
from bisect import bisect_right
from datetime import datetime, timedelta

from earthwave.iasp91_table import DEPTHS_KM, DISTANCES_DEG, P_SECONDS, S_SECONDS
from earthwave.sources.geo import EARTH_RADIUS_KM

KM_PER_DEGREE = math.pi * EARTH_RADIUS_KM / 180.0
JOULES_PER_TON_TNT = 4.184e9
ENERGY_PER_MAGNITUDE_STEP = 10**1.5  # 31.6x


def energy_joules(mag: float) -> float:
    """Radiated seismic energy, E = 10^(4.8 + 1.5 M) joules (the USGS Gutenberg-Richter relation)."""
    return 10 ** (4.8 + 1.5 * mag)


def energy_ratio(m1: float, m2: float) -> float:
    """How many times more energy an M`m2` quake radiates than an M`m1` one: 10^(1.5 (m2 - m1))."""
    return 10 ** (1.5 * (m2 - m1))


def tnt_tons(mag: float) -> float:
    return energy_joules(mag) / JOULES_PER_TON_TNT


def fmt_tnt(tons: float) -> str:
    """A TNT-equivalent mass, e.g. '12 kg', '480 t', '3.2 kt', '1.2 Mt'."""
    for unit, size in (("Gt", 1e9), ("Mt", 1e6), ("kt", 1e3), ("t", 1.0)):
        if tons >= size:
            value = tons / size
            return f"{value:.0f} {unit}" if value >= 10 else f"{value:.1f} {unit}"
    kg = tons * 1e3
    return f"{kg:.0f} kg" if kg >= 10 else f"{kg:.1f} kg"


def _bracket(axis: tuple[float, ...], value: float) -> tuple[int, float]:
    """Index of the cell below `value` and the fraction across it; clamped to the axis ends."""
    value = min(max(value, axis[0]), axis[-1])
    i = min(bisect_right(axis, value) - 1, len(axis) - 2)
    return i, (value - axis[i]) / (axis[i + 1] - axis[i])


def _bilinear(table: tuple[tuple[float, ...], ...], di: int, df: float, zi: int, zf: float) -> float:
    def row(z: int) -> float:
        return table[z][di] * (1 - df) + table[z][di + 1] * df

    return row(zi) * (1 - zf) + row(zi + 1) * zf


def travel_times(distance_km: float, depth_km: float) -> tuple[float, float]:
    """(P, S) first-arrival travel times in seconds, bilinear in the embedded IASP91 table.

    Distance clamps to 0..180 degrees (the antipode) and depth to 0..700 km. Past about 104 degrees there is
    no direct S: S is the first S-type arrival, SKS or the core-converted PKIKS at the antipode.
    """
    di, df = _bracket(DISTANCES_DEG, distance_km / KM_PER_DEGREE)
    zi, zf = _bracket(DEPTHS_KM, depth_km)
    return _bilinear(P_SECONDS, di, df, zi, zf), _bilinear(S_SECONDS, di, df, zi, zf)


def arrivals(distance_km: float, depth_km: float, origin: datetime) -> tuple[datetime, datetime]:
    """(P, S) arrival times: the origin time plus each travel time."""
    p, s = travel_times(distance_km, depth_km)
    return origin + timedelta(seconds=p), origin + timedelta(seconds=s)
