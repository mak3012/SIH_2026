"""Depth helpers used across the repo. Implementations live in core.canonical; this module keeps
the short names older callers import.

- minimum_curvature, Station, Position: core.canonical.survey
- tvdss(tvd, elevation): TVD below the datum -> TVD sub-sea (see core.canonical.datum)
- offset_lonlat: geodesic offset on WGS 84 (core.canonical.crs.offset_to_lonlat)
- at_md / at_tvd: linear interpolation between computed positions, for callers that only
  have positions. With the stations, use core.canonical.survey.position_at / md_at_tvd, which
  follow the minimum-curvature arc.
"""

from core.canonical.crs import offset_to_lonlat as offset_lonlat
from core.canonical.survey import Position, Station, minimum_curvature

__all__ = ["Position", "Station", "at_md", "at_tvd", "minimum_curvature", "offset_lonlat", "tvdss"]


def tvdss(tvd: float, rt_elevation: float) -> float:
    """TVD below RT -> TVD sub-sea. rt_elevation is RT height above MSL (positive up)."""
    return tvd - rt_elevation


def at_tvd(path: list[Position], tvd: float) -> Position:
    """Position where the path first reaches `tvd` (linear between stations)."""
    for a, b in zip(path, path[1:], strict=False):
        if a.tvd <= tvd <= b.tvd and b.tvd > a.tvd:
            return _lerp(a, b, (tvd - a.tvd) / (b.tvd - a.tvd))
    raise ValueError(f"path does not reach TVD {tvd}")


def at_md(path: list[Position], md: float) -> Position:
    """Position at `md` (linear between stations)."""
    for a, b in zip(path, path[1:], strict=False):
        if a.md <= md <= b.md and b.md > a.md:
            return _lerp(a, b, (md - a.md) / (b.md - a.md))
    raise ValueError(f"MD {md} outside the path")


def _lerp(a: Position, b: Position, f: float) -> Position:
    return Position(
        a.md + f * (b.md - a.md),
        a.tvd + f * (b.tvd - a.tvd),
        a.north + f * (b.north - a.north),
        a.east + f * (b.east - a.east),
    )
