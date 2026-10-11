"""Depth datums: what a depth is measured from, and TVDSS.

Every wellbore stores its depth reference: the type (KB kelly bushing, RT rotary table, DF drill
floor, GL ground level, MSL) and that point's elevation above mean sea level in metres. Two
reports of one well can use different references (a workover rig's KB is not the drilling rig's
RT), so a depth without its datum is not comparable with anything.

    TVDSS = TVD - datum elevation        (positive down, below MSL)

A depth below one datum moves to another by the difference of their elevations. MD shifts the
same way, because both references sit on the same vertical axis above the wellhead.
"""

from dataclasses import dataclass
from enum import StrEnum


class DatumType(StrEnum):
    KB = "KB"  # kelly bushing
    RT = "RT"  # rotary table
    DF = "DF"  # drill floor
    GL = "GL"  # ground level
    MSL = "MSL"  # mean sea level


@dataclass(frozen=True)
class DepthDatum:
    type: DatumType
    elevation_m: float  # above MSL; negative below (e.g. GL on a lake bed)

    def __post_init__(self) -> None:
        if self.type == DatumType.MSL and self.elevation_m != 0:
            raise ValueError("MSL datum has elevation 0 by definition")

    def __str__(self) -> str:
        return f"{self.type} {self.elevation_m:+.2f} m AMSL"


MSL = DepthDatum(DatumType.MSL, 0.0)


def tvdss(tvd: float, datum: DepthDatum) -> float:
    """TVD below `datum` -> TVD below mean sea level."""
    return tvd - datum.elevation_m


def tvd_from_tvdss(value: float, datum: DepthDatum) -> float:
    return value + datum.elevation_m


def rebase(depth: float, source: DepthDatum, target: DepthDatum) -> float:
    """A depth (MD or TVD) below `source` expressed below `target` on the same wellbore."""
    return depth - source.elevation_m + target.elevation_m
