"""Live top re-anchoring: a geologist picks a top, every hazard of that well moves with it.

Hazards (hazard_profile rows) are stored formation-relatively: formation + rel_top/rel_base,
plus the absolute depths last computed from them. A pick changes the formation's top (and the
base of the formation above it), so this recomputes every hazard's TVDSS and MD from its rel
interval against the well's current tops and survey. Hazards whose formation has no top, or whose
formation's base is unknown, keep their depths and are reported.
"""

from dataclasses import dataclass, field

from core.canonical.datum import DepthDatum, tvd_from_tvdss
from core.canonical.position import Top, at_rel, intervals
from core.canonical.survey import Station, md_at_tvd


@dataclass(frozen=True)
class Hazard:
    id: int
    formation: str
    rel_top: float
    rel_base: float
    tvdss_top: float | None = None
    tvdss_base: float | None = None
    md_top: float | None = None
    md_base: float | None = None


@dataclass
class Change:
    hazard_id: int
    before: tuple[float | None, float | None]  # tvdss top, base
    after: tuple[float | None, float | None]
    md: tuple[float | None, float | None]
    moved_m: float | None
    notes: list[str] = field(default_factory=list)


def pick(tops: list[Top], formation: str, tvdss: float) -> list[Top]:
    """The well's tops with `formation` (re)picked at `tvdss`."""
    return sorted(
        [t for t in tops if t.formation != formation] + [Top(formation, tvdss)],
        key=lambda t: t.tvdss,
    )


def reproject(
    hazards: list[Hazard], tops: list[Top], survey: list[Station] | None, datum: DepthDatum | None
) -> list[Change]:
    ivs = {iv.formation: iv for iv in intervals(tops)}
    out = []
    for h in hazards:
        iv = ivs.get(h.formation)
        before = (h.tvdss_top, h.tvdss_base)
        if iv is None or iv.base is None:
            why = "no top for its formation" if iv is None else "formation base unknown"
            out.append(Change(h.id, before, before, (h.md_top, h.md_base), None, [f"kept: {why}"]))
            continue
        top, base = at_rel(iv, h.rel_top), at_rel(iv, h.rel_base)
        md: tuple[float | None, float | None] = (None, None)
        notes = []
        if survey and datum is not None:
            try:
                md = (
                    md_at_tvd(survey, tvd_from_tvdss(top, datum)),
                    md_at_tvd(survey, tvd_from_tvdss(base, datum)),
                )
            except ValueError:
                notes.append("interval beyond the survey: MD not updated")
        moved = abs(top - h.tvdss_top) if h.tvdss_top is not None else None
        out.append(Change(h.id, before, (top, base), md, moved, notes))
    return out
