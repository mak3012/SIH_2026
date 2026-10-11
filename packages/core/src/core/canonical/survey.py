"""Directional surveys: minimum curvature, and position at any measured depth.

Conventions: MD, TVD, north, east in metres (convert first); inclination 0 = vertical down, 90
= horizontal; azimuth clockwise from north, in whatever north the survey uses (the reference is
carried separately; see crs.offset_to_lonlat). TVD is measured from the survey's depth datum
(see datum.py for TVDSS).

Minimum curvature assumes the hole between two stations is a circular arc in one plane. It is the
industry standard, and exact for such arcs. Interpolating at an MD between stations follows the
same arc (spherical interpolation of the tangent), so an interpolated point lies on the path
that the station positions describe; straight-line interpolation of TVD does not.
"""

from dataclasses import dataclass
from math import acos, atan2, cos, degrees, radians, sin, tan

M_PER_30M = 30.0


@dataclass(frozen=True)
class Station:
    md: float
    inc: float
    azi: float


@dataclass(frozen=True)
class Position:
    md: float
    tvd: float
    north: float
    east: float


def tangent(inc: float, azi: float) -> tuple[float, float, float]:
    """Unit vector along the hole: (north, east, down)."""
    i, a = radians(inc), radians(azi)
    return sin(i) * cos(a), sin(i) * sin(a), cos(i)


def angles(t: tuple[float, float, float]) -> tuple[float, float]:
    """(inc, azi) in degrees of a (not necessarily unit) tangent vector."""
    n, e, d = t
    norm = (n * n + e * e + d * d) ** 0.5
    inc = degrees(acos(max(-1.0, min(1.0, d / norm))))
    azi = degrees(atan2(e, n)) % 360 if (n * n + e * e) > 1e-24 else 0.0
    return inc, azi


def dogleg(a: Station, b: Station) -> float:
    """Angle between the two stations' tangents, radians."""
    t1, t2 = tangent(a.inc, a.azi), tangent(b.inc, b.azi)
    return acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(t1, t2, strict=True)))))


def dls(a: Station, b: Station, per: float = M_PER_30M) -> float:
    """Dogleg severity, degrees per `per` metres (30 m by default)."""
    return degrees(dogleg(a, b)) * per / (b.md - a.md) if b.md > a.md else 0.0


def step(p: Position, a: Station, b: Station) -> Position:
    """Position at station b, given position p at station a."""
    beta = dogleg(a, b)
    rf = 1.0 if beta < 1e-9 else (2 / beta) * tan(beta / 2)
    half = (b.md - a.md) / 2 * rf
    t1, t2 = tangent(a.inc, a.azi), tangent(b.inc, b.azi)
    return Position(
        b.md,
        p.tvd + half * (t1[2] + t2[2]),
        p.north + half * (t1[0] + t2[0]),
        p.east + half * (t1[1] + t2[1]),
    )


def minimum_curvature(stations: list[Station], tie_in: Position | None = None) -> list[Position]:
    """Positions of all stations. Without a tie-in the first station is at TVD = MD, N = E = 0
    (a vertical hole above it), which is right when the first station is at surface."""
    if not stations:
        return []
    first = stations[0]
    out = [tie_in or Position(first.md, first.md, 0.0, 0.0)]
    for a, b in zip(stations, stations[1:], strict=False):
        out.append(step(out[-1], a, b))
    return out


def station_at(stations: list[Station], md: float) -> Station:
    """Inclination and azimuth at `md`, on the arc between the neighbouring stations."""
    a, b = _segment(stations, md)
    beta = dogleg(a, b)
    f = (md - a.md) / (b.md - a.md) if b.md > a.md else 0.0
    t1, t2 = tangent(a.inc, a.azi), tangent(b.inc, b.azi)
    if beta < 1e-9:
        t = t1
    else:
        w1, w2 = sin((1 - f) * beta) / sin(beta), sin(f * beta) / sin(beta)
        t = (w1 * t1[0] + w2 * t2[0], w1 * t1[1] + w2 * t2[1], w1 * t1[2] + w2 * t2[2])
    inc, azi = angles(t)
    return Station(md, inc, azi)


def position_at(stations: list[Station], md: float, path: list[Position] | None = None) -> Position:
    """Position at any MD within the survey (on the minimum-curvature arc)."""
    path = path or minimum_curvature(stations)
    i = _index(stations, md)
    if stations[i].md == md:
        return path[i]
    return step(path[i], stations[i], station_at(stations, md))


def tvd_at(stations: list[Station], md: float, path: list[Position] | None = None) -> float:
    return position_at(stations, md, path).tvd


def md_at_tvd(stations: list[Station], tvd: float, path: list[Position] | None = None) -> float:
    """First MD at which the path reaches `tvd` (bisection on the arc; 1e-6 m)."""
    path = path or minimum_curvature(stations)
    for i in range(len(path) - 1):
        lo, hi = path[i], path[i + 1]
        if min(lo.tvd, hi.tvd) <= tvd <= max(lo.tvd, hi.tvd) and hi.md > lo.md:
            a, b = lo.md, hi.md
            rising = hi.tvd >= lo.tvd
            for _ in range(80):
                mid = (a + b) / 2
                if (position_at(stations, mid, path).tvd < tvd) == rising:
                    a = mid
                else:
                    b = mid
                if b - a < 1e-6:
                    break
            return (a + b) / 2
    raise ValueError(f"survey does not reach TVD {tvd}")


def _index(stations: list[Station], md: float) -> int:
    if not stations or md < stations[0].md or md > stations[-1].md:
        raise ValueError(f"MD {md} outside the survey")
    for i in range(len(stations) - 1):
        if stations[i].md <= md < stations[i + 1].md:
            return i
    return len(stations) - 1


def _segment(stations: list[Station], md: float) -> tuple[Station, Station]:
    i = _index(stations, md)
    if i == len(stations) - 1:
        return stations[i - 1] if i else stations[i], stations[i]
    return stations[i], stations[i + 1]


__all__ = [
    "Position",
    "Station",
    "angles",
    "dls",
    "dogleg",
    "md_at_tvd",
    "minimum_curvature",
    "position_at",
    "station_at",
    "step",
    "tangent",
    "tvd_at",
]
