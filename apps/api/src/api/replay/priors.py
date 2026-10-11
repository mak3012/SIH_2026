"""Priors for a held-out well from the other wells only.

    analogues: an.candidates / an.rank around the held-out well, which never includes the well
               itself (candidates exclude the active well);
    roadmap:   hz.roadmap over those offsets;
    prior:     basin rates computed with the held-out wellbore excluded.

The held-out well's own formation tops place the formation intervals (in a real plan these come
from the prognosis); its own events never enter a number.
"""

from dataclasses import dataclass, field
from typing import Any

import psycopg

from api.intelligence import analogues as an
from api.intelligence import hazards as hz
from api.intelligence import repo

SURFACE_M = 50_000.0
SUBSURFACE_M = 20_000.0
MAX_OFFSETS = 10


@dataclass
class LooPriors:
    wellbore_id: int
    target: str | None
    offsets: list[tuple[str, float]] = field(default_factory=list)  # (well, analogue score)
    priors: dict[tuple[str, str], tuple[float, str]] = field(default_factory=dict)
    evidence: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    cells: list[hz.Cell] = field(default_factory=list)


def target_formation(forms: list[hz.ActiveFormation]) -> str | None:
    """The formation analogues are compared at: the middle one with a known top (where most of
    the hole is), the same rule for every held-out well."""
    named = [f for f in forms if f.top_md is not None]
    return named[len(named) // 2].formation if named else None


def loo_priors(conn: psycopg.Connection, wellbore_id: int) -> LooPriors:
    forms = repo.active_formations(conn, wellbore_id, None)
    target = target_formation(forms)
    out = LooPriors(wellbore_id, target)
    if target is None:
        return out
    fid = conn.execute(
        "SELECT id FROM formation WHERE name = %s ORDER BY id LIMIT 1", (target,)
    ).fetchone()
    assert fid is not None
    active = an.active_context(conn, wellbore_id, int(fid[0]))
    cands = an.candidates(conn, active, surface_m=SURFACE_M)
    ranked = an.rank(
        active,
        cands,
        {},
        subsurface_m=SUBSURFACE_M,
        pinned=set(),
        excluded=set(),
        max_offsets=MAX_OFFSETS,
    )
    chosen = [(r.features.well_id, r.score) for r in ranked if r.included]
    if not chosen:  # nothing within the radii: the nearest wells at surface, scored as usual
        chosen = [(r.features.well_id, r.score) for r in ranked[:MAX_OFFSETS]]
    names = {r.features.well_id: r.features.well for r in ranked}
    out.offsets = [(names[w], round(s, 3)) for w, s in chosen]
    offs = repo.offsets(conn, chosen)
    rates = repo.basin_rates(conn, exclude_wellbores=[wellbore_id])
    out.cells = hz.roadmap(forms, offs, basin_rates=rates)
    for c in out.cells:
        key = (c.formation, c.hazard)
        src = f"leave-one-out roadmap: mean {c.mean:.3f}, n_eff {c.n_eff:.1f}"
        out.priors[key] = (
            c.mean,
            src + (" (prior only)" if c.evidence_state == "no_evidence" else ""),
        )
        out.evidence[key] = [
            {k: e.get(k) for k in ("event_id", "well", "md", "severity", "document", "page")}
            for e in c.evidence
        ]
    return out
