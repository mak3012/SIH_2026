"""Formation-relative position, and projecting an offset event onto the active well.

    rel = (tvdss - top) / (base - top)        0 at the top, 1 at the base

Formations dip and thin. An event at 2,610 m TVDSS in an offset well, 10 m below the Barail top
there, is a Barail-top event. In the active well the Barail top may be at 2,760 m: the same
TVDSS (2,610 m) is still in the Tipam there ("same depth, wrong rock"). Projecting by rel keeps
the event in the same rock ("same formation"): rel is computed in the offset well and applied
to the formation's top and base in the active well.

When a formation's base is unknown in either well (the deepest formation reached), rel cannot be
formed; the projection then keeps the distance below the top (method `below_top`) and says so.
"""

from dataclasses import dataclass, field

from core.canonical.datum import DepthDatum, tvd_from_tvdss
from core.canonical.survey import Station, md_at_tvd


@dataclass(frozen=True)
class Top:
    formation: str
    tvdss: float


@dataclass(frozen=True)
class Interval:
    formation: str
    top: float
    base: float | None  # None: the next top is not known (deepest formation reached)


def intervals(tops: list[Top]) -> list[Interval]:
    """Formation intervals from a well's tops: each base is the next top down."""
    s = sorted(tops, key=lambda t: t.tvdss)
    return [
        Interval(t.formation, t.tvdss, s[i + 1].tvdss if i + 1 < len(s) else None)
        for i, t in enumerate(s)
    ]


def rel_position(tvdss: float, top: float, base: float) -> float:
    if base <= top:
        raise ValueError(f"base {base} must be deeper than top {top}")
    return (tvdss - top) / (base - top)


def at_rel(interval: Interval, rel: float) -> float:
    if interval.base is None:
        raise ValueError(f"{interval.formation}: base unknown")
    return interval.top + rel * (interval.base - interval.top)


def locate(tvdss: float, tops: list[Top]) -> tuple[Interval | None, float | None]:
    """Formation containing a depth, and the relative position in it (None when base unknown)."""
    for iv in intervals(tops):
        if tvdss >= iv.top and (iv.base is None or tvdss < iv.base):
            return iv, (rel_position(tvdss, iv.top, iv.base) if iv.base is not None else None)
    return None, None


@dataclass
class Projection:
    formation: str | None
    rel: float | None
    offset_tvdss: float
    tvdss: float | None  # projected into the active well
    md: float | None  # along the active well's survey, when given
    method: str  # relative | below_top | absent | above_first_top
    naive_formation: str | None  # the rock at the same TVDSS in the active well
    notes: list[str] = field(default_factory=list)


def project(
    offset_tvdss: float,
    offset_tops: list[Top],
    active_tops: list[Top],
    active_survey: list[Station] | None = None,
    active_datum: DepthDatum | None = None,
) -> Projection:
    """Where an event seen in an offset well would sit, formation-relatively, in the active
    well. Active tops may be picks or a prognosis; the survey may be the well plan."""
    naive, _ = locate(offset_tvdss, active_tops)
    iv, rel = locate(offset_tvdss, offset_tops)
    if iv is None:
        return Projection(
            None,
            None,
            offset_tvdss,
            None,
            None,
            "above_first_top",
            naive.formation if naive else None,
            ["depth is above the first known top in the offset well"],
        )
    target = next((a for a in intervals(active_tops) if a.formation == iv.formation), None)
    if target is None:
        return Projection(
            iv.formation,
            rel,
            offset_tvdss,
            None,
            None,
            "absent",
            naive.formation if naive else None,
            [f"{iv.formation} has no top in the active well (absent or not yet picked)"],
        )
    notes: list[str] = []
    if rel is not None and target.base is not None:
        tvdss, method = at_rel(target, rel), "relative"
    else:
        tvdss, method = target.top + (offset_tvdss - iv.top), "below_top"
        notes.append("formation base unknown in one of the wells: kept the distance below top")
    md = None
    if active_survey and active_datum is not None:
        try:
            md = md_at_tvd(active_survey, tvd_from_tvdss(tvdss, active_datum))
        except ValueError:
            notes.append("projected depth is beyond the active well's survey")
    return Projection(
        iv.formation,
        rel,
        offset_tvdss,
        tvdss,
        md,
        method,
        naive.formation if naive else None,
        notes,
    )
