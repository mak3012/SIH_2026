"""Build a pipeline Context: from the database for a live wellbore, or from a synthetic Plan.

Database context (read as the service owner, not under a user's RLS scope):
- trajectory from survey_station; formation tops (human pick wins) from formation_top;
- casing shoes from casing_string, hole sizes from the latest drilling program, else bit runs;
- mud weight and rheology (PV/YP -> Bingham as Herschel-Bulkley n = 1) from the latest
  mud_record;
- friction factor calibrated on offset wells' tripping data (weighted median by analogue
  score), else the default 0.25;
- priors per (formation, hazard) from the hazard roadmap of the latest offset selection, with
  the offset evidence (document, page) the explainer cites.
"""

import logging
from typing import Any

import numpy as np
import psycopg

from api.intelligence import analogues as an
from api.intelligence import hazards as hz
from api.intelligence import repo
from api.realtime.derived import Rheology, Trajectory, calibrate_friction
from api.realtime.pipeline import Context
from api.realtime.synthetic import Plan

log = logging.getLogger(__name__)
MAX_FRICTION_POINTS = 200


def context_from_plan(plan: Plan, wellbore_id: int = 0) -> Context:
    return Context(
        wellbore_id=wellbore_id,
        traj=Trajectory.from_stations(plan.survey),
        tops=list(plan.tops),
        shoes=list(plan.shoes),
        holes=[(0.0, plan.hole_in)],
        mw_sg=plan.mw_sg,
    )


def _stations(conn: psycopg.Connection, wellbore_id: int) -> list[tuple[float, float, float]]:
    return [
        (float(a), float(b), float(c))
        for a, b, c in conn.execute(
            "SELECT md, inc, azi FROM survey_station WHERE wellbore_id = %s ORDER BY md",
            (wellbore_id,),
        )
    ]


def offset_friction(
    conn: psycopg.Connection, offsets: list[tuple[int, float]]
) -> tuple[float, str] | None:
    """Weighted median of friction factors fitted to each offset's tripping hookloads.
    `offsets` = (well id, analogue weight)."""
    fits: list[tuple[float, float, str]] = []
    for well_id, weight in offsets:
        row = conn.execute(
            "SELECT wb.id, w.name FROM wellbore wb JOIN well w ON w.id = wb.well_id"
            " WHERE wb.well_id = %s ORDER BY wb.id LIMIT 1",
            (well_id,),
        ).fetchone()
        if row is None:
            continue
        pts = conn.execute(
            "SELECT bit_md, rig_state::text, hookload_kn, coalesce(mw_in, 1.2) FROM rt_measurement"
            " WHERE wellbore_id = %s AND rig_state IN ('tripping_out', 'tripping_in')"
            " AND hookload_kn IS NOT NULL AND bit_md > 200 ORDER BY random() LIMIT %s",
            (row[0], MAX_FRICTION_POINTS),
        ).fetchall()
        if len(pts) < 20:
            continue
        traj = Trajectory.from_stations(_stations(conn, row[0]))
        mu = calibrate_friction(
            traj,
            [
                (float(md), "pickup" if st == "tripping_out" else "slackoff", float(hl), float(mw))
                for md, st, hl, mw in pts
            ],
        )
        if mu is not None:
            fits.append((mu, weight, str(row[1])))
    if not fits:
        return None
    fits.sort()
    w = np.cumsum([f[1] for f in fits])
    mu = fits[int(np.searchsorted(w, w[-1] / 2))][0]
    return mu, "offsets " + ", ".join(f"{n} {m:.2f}" for m, _, n in fits)


def load(conn: psycopg.Connection, wellbore_id: int) -> Context:
    traj = Trajectory.from_stations(_stations(conn, wellbore_id))
    ctx = Context(wellbore_id=wellbore_id, traj=traj)
    try:
        forms = repo.active_formations(conn, wellbore_id, None)
        ctx.tops = [(f.formation, f.top_md) for f in forms if f.top_md is not None]
    except LookupError:
        forms = []
    ctx.shoes = [
        (float(md), float(od))
        for md, od in conn.execute(
            "SELECT md_shoe, od_in FROM casing_string WHERE wellbore_id = %s ORDER BY md_shoe",
            (wellbore_id,),
        )
    ]
    prog = conn.execute(
        "SELECT program FROM drilling_program WHERE wellbore_id = %s ORDER BY id DESC LIMIT 1",
        (wellbore_id,),
    ).fetchone()
    if prog:
        ctx.holes = sorted(
            (float(s["md_top"]), float(s["hole_in"])) for s in prog[0].get("sections", [])
        )
    else:
        ctx.holes = [
            (float(md), float(sz))
            for md, sz in conn.execute(
                "SELECT md_in, bit_size_in FROM bit_run WHERE wellbore_id = %s ORDER BY md_in",
                (wellbore_id,),
            )
        ]
    mud = conn.execute(
        "SELECT mw, pv_cp, yp_lbf100ft2 FROM mud_record WHERE wellbore_id = %s"
        " ORDER BY ts DESC LIMIT 1",
        (wellbore_id,),
    ).fetchone()
    if mud:
        ctx.mw_sg = float(mud[0])
        if mud[1] is not None and mud[2] is not None:
            ctx.rheology = Rheology.from_pv_yp(float(mud[1]), float(mud[2]))
    sel = conn.execute(
        "SELECT id FROM offset_selection WHERE active_wellbore_id = %s ORDER BY id DESC LIMIT 1",
        (wellbore_id,),
    ).fetchone()
    if sel and forms:
        weights = an.selected_offsets(conn, int(sel[0]))
        offs = repo.offsets(conn, weights)
        for cell in hz.roadmap(forms, offs, basin_rates=repo.basin_rates(conn)):
            key = (cell.formation, cell.hazard)
            src = f"roadmap selection {sel[0]}: mean {cell.mean:.3f}, n_eff {cell.n_eff:.1f}"
            if cell.evidence_state == "no_evidence":
                src += " (prior only)"
            ctx.priors[key] = (cell.mean, src)
            ctx.roadmap_evidence[key] = [_cite(e) for e in cell.evidence]
        fr = offset_friction(conn, weights)
        if fr:
            ctx.mu, ctx.mu_source = fr
    log.info(
        "context wellbore %s: %d tops, mu %.2f (%s)",
        wellbore_id,
        len(ctx.tops),
        ctx.mu,
        ctx.mu_source,
    )
    return ctx


def _cite(e: dict[str, Any]) -> dict[str, Any]:
    keys = ("event_id", "well", "md", "severity", "document", "page", "source_ref_id")
    return {k: e.get(k) for k in keys}
