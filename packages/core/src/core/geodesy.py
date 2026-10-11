"""Datum shifts for legacy Indian coordinates.

Kalianpur 1975 (EPSG:4146) on the Everest 1830 (1975 definition) ellipsoid, shifted to WGS 84
with the 3-parameter geocentric translation EPSG:1156 (dX 295, dY 736, dZ 257 m).
The published accuracy of that transformation is metres to tens of metres; good enough to place
a well, not for survey-grade work.
"""

from math import atan2, cos, degrees, radians, sin, sqrt

WGS84 = (6378137.0, 1 / 298.257223563)
EVEREST_1975 = (6377299.151, 1 / 300.8017255)
KALIANPUR_1975_TO_WGS84 = (295.0, 736.0, 257.0)


def _to_ecef(lat: float, lon: float, h: float, ell: tuple[float, float]) -> tuple[float, ...]:
    a, f = ell
    e2 = f * (2 - f)
    phi, lam = radians(lat), radians(lon)
    n = a / sqrt(1 - e2 * sin(phi) ** 2)
    return (
        (n + h) * cos(phi) * cos(lam),
        (n + h) * cos(phi) * sin(lam),
        (n * (1 - e2) + h) * sin(phi),
    )


def _from_ecef(x: float, y: float, z: float, ell: tuple[float, float]) -> tuple[float, float]:
    a, f = ell
    e2 = f * (2 - f)
    p = sqrt(x * x + y * y)
    phi = atan2(z, p * (1 - e2))
    for _ in range(6):  # converges to < 1e-12 rad in 3-4 iterations
        n = a / sqrt(1 - e2 * sin(phi) ** 2)
        h = p / cos(phi) - n
        phi = atan2(z, p * (1 - e2 * n / (n + h)))
    return degrees(phi), degrees(atan2(y, x))


def kalianpur1975_to_wgs84(lat: float, lon: float) -> tuple[float, float]:
    x, y, z = _to_ecef(lat, lon, 0.0, EVEREST_1975)
    dx, dy, dz = KALIANPUR_1975_TO_WGS84
    return _from_ecef(x + dx, y + dy, z + dz, WGS84)


def wgs84_to_kalianpur1975(lat: float, lon: float) -> tuple[float, float]:
    x, y, z = _to_ecef(lat, lon, 0.0, WGS84)
    dx, dy, dz = KALIANPUR_1975_TO_WGS84
    return _from_ecef(x - dx, y - dy, z - dz, EVEREST_1975)


def to_dms(value: float, pos: str, neg: str) -> str:
    """27.1034 -> 27°06'12.24\"N"""
    hemi = pos if value >= 0 else neg
    v = abs(value)
    d = int(v)
    m = int((v - d) * 60)
    s = (v - d - m / 60) * 3600
    if round(s, 2) >= 60:
        m, s = m + 1, 0.0
    if m == 60:
        d, m = d + 1, 0
    return f"{d}°{m:02d}'{s:05.2f}\"{hemi}"
