"""Seed 3 demo formations and 5 synthetic wells (Upper Assam-like, all values invented).

Usage: python -m api.seed   (connects with DATABASE_URL as the table owner; bypasses RLS)

Layout, chosen so the example queries in docs/data-model.md return something:
  DEMO-1..3 in block AA-ONN-2001/1, DEMO-4..5 in AA-ONN-2001/2 (DEMO-5 also JV-DEMO).
  DEMO-2 (~3 km) and DEMO-3 (~2 km) lie within 5 km of DEMO-1; DEMO-4 (~7 km) and
  DEMO-5 (~12 km) do not. Losses at the Barail top on DEMO-1, -2 and -4.
  DEMO-3 sticks pipe in the Kopili and is sidetracked (DEMO-3 ST1).
"""

import hashlib
import os
import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from core.depth import Position, Station, at_tvd, minimum_curvature, offset_lonlat, tvdss

BASIN = "Upper Assam"
B1, B2, JV = "AA-ONN-2001/1", "AA-ONN-2001/2", "JV-DEMO"
FORMATIONS = {  # name -> (synonyms, top TVDSS at DEMO-1, thickness m)
    "Tipam": (["TIPAM SST", "Tipam Sandstone"], 1750.0, 850.0),
    "Barail": (["BARAIL", "Barail Group", "Barail Coal Shale"], 2600.0, 650.0),
    "Kopili": (["KOPILI SH", "Kopili Shale"], 3250.0, 400.0),
}


@dataclass
class Ev:
    type: str
    formation: str
    rel_pos: float
    npt_hr: float
    severity: int
    narrative: str
    outcome: str
    root_cause: str | None = None
    root_cause_status: str = "unknown"
    loss_bbl: float | None = None
    mw: float | None = None
    ecd: float | None = None
    mitigations: list[str] = field(default_factory=list)


@dataclass
class WellSpec:
    name: str
    block: str
    jv: str | None
    lon: float
    lat: float
    rt: float
    kop: float | None = None  # None = vertical
    build: float = 0.0  # deg / 30 m
    max_inc: float = 0.0
    azi: float = 0.0
    events: list[Ev] = field(default_factory=list)


LOSS = "Lost returns while drilling into the Barail top."
WELLS = [
    WellSpec("DEMO-1", B1, None, 94.900, 27.100, 118, events=[
        Ev("loss", "Barail", 0.03, 14, 3, LOSS, "Cured with LCM; drilled ahead at reduced flow",
           "Fractured coal/sand stringers at Barail top; ECD above fracture gradient",
           "recorded", 180, 1.28, 1.36,
           ["Pumped 40 bbl medium LCM pill", "Reduced flow rate by 15 percent"]),
    ]),
    WellSpec("DEMO-2", B1, None, 94.930, 27.110, 121, 900, 2.0, 30, 45, events=[
        Ev("loss", "Barail", 0.05, 31, 4, LOSS + " Total losses for 6 h.",
           "Partial cure; 9-5/8 in casing set early", None, "unknown", 420, 1.30, 1.39,
           ["Pumped two LCM pills", "Set cement plug across loss zone"]),
    ]),
    WellSpec("DEMO-3", B1, None, 94.915, 27.085, 115, 1200, 2.5, 35, 200, events=[
        Ev("stuck_pipe", "Kopili", 0.2, 52, 4, "Pipe stuck after connection; could not rotate.",
           "Fishing unsuccessful; sidetracked as DEMO-3 ST1",
           "Differential sticking across depleted sand", "inferred", None, 1.34, 1.40,
           ["Jarred down for 6 h", "Spotted pipe-freeing pill"]),
    ]),
    WellSpec("DEMO-4", B2, None, 94.960, 27.070, 124, 800, 3.0, 40, 110, events=[
        Ev("loss", "Barail", 0.01, 6, 2, LOSS + " Seepage on connections.",
           "Cured by reducing surge", "Surge pressure on connections induced fractures",
           "recorded", 95, 1.27, 1.33, ["Reduced pipe running speed"]),
    ]),
    WellSpec("DEMO-5", B2, JV, 95.020, 27.060, 112, events=[
        Ev("tight_hole", "Tipam", 0.6, 4, 2, "Overpull 20 t on trip out through Tipam sands.",
           "Reamed through; no further issues", mitigations=["Back-reamed the interval"]),
        Ev("kick", "Kopili", 0.1, 18, 5, "Pit gain 12 bbl after trip; well shut in.",
           "Circulated out; mud weight raised to 1.42 sg",
           "Swabbing on trip out left the well underbalanced", "inferred", None, 1.35, 1.38,
           ["Shut in well", "Circulated kick out with driller's method"]),
    ]),
]  # fmt: skip


def survey(spec: WellSpec, md_max: float = 4500) -> list[Station]:
    stations, inc, md = [], 0.0, 0.0
    while md <= md_max:
        if spec.kop is not None and md > spec.kop:
            inc = min(spec.max_inc, inc + spec.build)
        stations.append(Station(md, inc, spec.azi))
        md += 30
    return stations


def md_at_tvdss(pos: list[Position], rt: float, target: float) -> tuple[float, float]:
    """(md, tvd) where the path first reaches `target` TVDSS."""
    p = at_tvd(pos, target + rt)
    return p.md, p.tvd


def main() -> None:
    rng = random.Random(7)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        if conn.execute("SELECT 1 FROM well WHERE name = 'DEMO-1'").fetchone():
            print("already seeded")
            return

        def one(sql: str, params: tuple[Any, ...]) -> int:
            row = conn.execute(sql + " RETURNING id", params).fetchone()
            assert row is not None
            return int(row[0])

        conn.execute(
            "INSERT INTO block VALUES (%s, 'Demo Block North', %s), (%s, 'Demo Block South', %s)",
            (B1, BASIN, B2, BASIN),
        )
        conn.execute("INSERT INTO jv VALUES (%s, 'Demo Joint Venture', 'Demo Operator')", (JV,))
        conn.execute(
            "INSERT INTO basin (id, name) VALUES ('Assam-Arakan', 'Assam-Arakan Basin'),"
            " (%s, 'Upper Assam Shelf') ON CONFLICT DO NOTHING",
            (BASIN,),
        )
        conn.execute("UPDATE basin SET parent_id = 'Assam-Arakan' WHERE id = %s", (BASIN,))
        fid = {
            name: one(
                "INSERT INTO formation (basin, name, synonyms, abbreviations, expected_tvdss_top,"
                " expected_tvdss_base) VALUES (%s, %s, %s, %s, %s, %s)",
                (BASIN, name, syn, [name[:3].upper()], top - 300, top + thick + 300),
            )
            for name, (syn, top, thick) in FORMATIONS.items()
        }

        for n, spec in enumerate(WELLS):
            spud = date(2024, 1, 15) + timedelta(days=75 * n)
            doc = one(
                "INSERT INTO document (sha256, type, title, vintage, uri, page_count, block_id,"
                " jv_id) VALUES (%s, 'wcr', %s, %s, %s, 60, %s, %s)",
                (
                    hashlib.sha256(f"synthetic WCR {spec.name}".encode()).digest(),
                    f"{spec.name} Well Completion Report (synthetic)",
                    spud + timedelta(days=90),
                    f"s3://documents/synthetic/{spec.name}-WCR.pdf",
                    spec.block,
                    spec.jv,
                ),
            )

            def src(page: int, doc: int = doc) -> int:
                y = rng.uniform(0.1, 0.7)
                return one(
                    "INSERT INTO source_ref (document_id, page, bbox, extractor_version,"
                    " confidence, review_status, reviewer, reviewed_at)"
                    " VALUES (%s, %s, %s, 'synthetic@1', 1.0, 'accepted', 'seed', now())",
                    (doc, page, [0.08, y, 0.92, y + 0.2]),
                )

            well = one(
                "INSERT INTO well (name, block_id, jv_id, field, basin, surface, rt_elevation_m,"
                " spud_date, status, source_ref_id) VALUES (%s, %s, %s, 'Demo Field', %s,"
                " ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, 'abandoned', %s)",
                (spec.name, spec.block, spec.jv, BASIN, spec.lon, spec.lat, spec.rt, spud, src(1)),
            )
            # Structural interpretation (invented): the two blocks are separate fault blocks.
            conn.execute("UPDATE well SET fault_block = %s WHERE id = %s",
                         ("FB-North" if spec.block == B1 else "FB-South", well))  # fmt: skip

            # Tops vary a little from well to well; the path is cut 300 m below the Kopili top.
            tops = {f: t + rng.uniform(-30, 30) for f, (_, t, _) in FORMATIONS.items()}
            stations = survey(spec)
            pos = minimum_curvature(stations)
            deep = tops["Kopili"] + 300
            keep = next(i for i, p in enumerate(pos) if tvdss(p.tvd, spec.rt) > deep)
            stations, pos = stations[: keep + 1], pos[: keep + 1]
            bores: list[tuple[str, float | None, list[Station], list[Position]]] = [
                (spec.name, None, stations, pos)
            ]

            if spec.name == "DEMO-3":  # sidetrack from 2900 m MD, turned 25 deg right
                kick = 2900.0
                st = [s for s in stations if s.md <= kick] + [
                    Station(s.md, s.inc, (s.azi + 25) % 360) for s in stations if s.md > kick
                ]
                bores.append((f"{spec.name} ST1", kick, st, minimum_curvature(st)))

            parent_id = None
            for bname, kickoff, sts, ps in bores:
                wb = one(
                    "INSERT INTO wellbore (well_id, name, parent_id, kickoff_md, td_md, status,"
                    " source_ref_id) VALUES (%s, %s, %s, %s, %s, 'completed', %s)",
                    (well, bname, parent_id if kickoff else None, kickoff, ps[-1].md, src(2)),
                )
                parent_id = parent_id or wb
                survey_src = src(12)
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO survey_station (wellbore_id, md, inc, azi, tvd, tvdss, geom,"
                        " source_ref_id) VALUES (%s, %s, %s, %s, %s, %s,"
                        " ST_SetSRID(ST_MakePoint(%s, %s, %s), 4326), %s)",
                        [
                            (
                                wb,
                                s.md,
                                s.inc,
                                s.azi,
                                p.tvd,
                                tvdss(p.tvd, spec.rt),
                                *offset_lonlat(spec.lon, spec.lat, p.north, p.east),
                                -tvdss(p.tvd, spec.rt),
                                survey_src,
                            )
                            for s, p in zip(sts, ps, strict=True)
                        ],  # fmt: skip
                    )
                for f, t in tops.items():
                    md, tvd = md_at_tvdss(ps, spec.rt, t)
                    if kickoff and md <= kickoff:
                        continue  # shared with the parent hole above the kickoff
                    conn.execute(
                        "INSERT INTO formation_top (wellbore_id, formation_id, md, tvd, tvdss,"
                        " source_ref_id) VALUES (%s, %s, %s, %s, %s, %s)",
                        (wb, fid[f], md, tvd, t, src(8)),
                    )
                if kickoff:
                    continue  # casing and events below belong to the original hole

                shoe_md, _ = md_at_tvdss(ps, spec.rt, tops["Barail"] - 40)
                lot_md, lot_tvd = md_at_tvdss(ps, spec.rt, tops["Barail"] - 35)
                losses = any(e.type == "loss" and e.loss_bbl and e.loss_bbl > 300
                             for e in spec.events)  # fmt: skip
                one(
                    "INSERT INTO casing_string (wellbore_id, kind, od_in, hole_size_in, weight_ppf,"
                    " grade, md_shoe, tvdss_shoe, set_date, source_ref_id)"
                    " VALUES (%s, 'surface', 13.375, 17.5, 68, 'K55', 500, %s, %s, %s)",
                    (wb, 500 - spec.rt, spud + timedelta(days=5), src(5)),
                )
                ic = one(
                    "INSERT INTO casing_string (wellbore_id, kind, od_in, hole_size_in, weight_ppf,"
                    " grade, md_shoe, tvdss_shoe, set_date, source_ref_id)"
                    " VALUES (%s, 'intermediate', 9.625, 12.25, 47, 'N80', %s, %s, %s, %s)",
                    (wb, shoe_md, tops["Barail"] - 40, spud + timedelta(days=30), src(5)),
                )
                conn.execute(
                    "INSERT INTO cement_job (wellbore_id, casing_string_id, md_top, md_base,"
                    " slurry_density_sg, volume_m3, returns, toc_md, job_date, source_ref_id)"
                    " VALUES (%s, %s, %s, %s, 1.90, 38, %s, %s, %s, %s)",
                    (wb, ic, shoe_md - 800, shoe_md, "partial" if losses else "full",
                     shoe_md - 800, spud + timedelta(days=31), src(6)),
                )  # fmt: skip
                conn.execute(
                    "INSERT INTO lot_fit (wellbore_id, casing_string_id, test_type, md, tvd, emw,"
                    " test_date, source_ref_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (wb, ic, "lot" if n % 2 else "fit", lot_md, lot_tvd,
                     round(rng.uniform(1.52, 1.62), 2), spud + timedelta(days=32), src(7)),
                )  # fmt: skip

                # The WCR's drilling-problems table covers the whole hole: an event type absent
                # from it is "none" there, not "unknown" (event_documentation).
                conn.execute(
                    "INSERT INTO event_documentation (wellbore_id, md_top, md_base, kind,"
                    " source_ref_id) VALUES (%s, 0, %s, 'wcr_problems', %s)",
                    (wb, ps[-1].md, src(20)),
                )
                conn.execute(
                    "INSERT INTO mud_record (wellbore_id, ts, md, mud_type, mw, source_ref_id)"
                    " VALUES (%s, %s, %s, 'wbm', %s, %s)",
                    (wb, datetime.combine(spud, datetime.min.time(), UTC) + timedelta(days=30),
                     shoe_md, 1.25, src(9)),
                )  # fmt: skip
                for k, e in enumerate(spec.events):
                    thick = FORMATIONS[e.formation][2]
                    top_ss = tops[e.formation] + e.rel_pos * thick
                    base_ss = top_ss + 5
                    md_top, _ = md_at_tvdss(ps, spec.rt, top_ss)
                    md_base, _ = md_at_tvdss(ps, spec.rt, base_ss)
                    t0 = datetime.combine(spud, datetime.min.time(), UTC) + timedelta(
                        days=35 + 10 * k, hours=rng.uniform(0, 20)
                    )
                    ev_src = src(20 + k)
                    ev = one(
                        "INSERT INTO event (wellbore_id, type, md_top, md_base, tvdss_top,"
                        " tvdss_base, formation_id, rel_pos, t_start, t_end, npt_hr, severity,"
                        " loss_bbl, mw, ecd, narrative, root_cause, root_cause_status, outcome,"
                        " source_ref_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                        " %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (wb, e.type, md_top, md_base, top_ss, base_ss, fid[e.formation],
                         e.rel_pos, t0, t0 + timedelta(hours=e.npt_hr), e.npt_hr, e.severity,
                         e.loss_bbl, e.mw, e.ecd, e.narrative, e.root_cause,
                         e.root_cause_status, e.outcome, ev_src),
                    )  # fmt: skip
                    for j, action in enumerate(e.mitigations):
                        conn.execute(
                            "INSERT INTO mitigation (event_id, action, t_applied, effective,"
                            " source_ref_id) VALUES (%s, %s, %s, %s, %s)",
                            (ev, action, t0 + timedelta(hours=j + 1), None, ev_src),
                        )
                    conn.execute(
                        "INSERT INTO chunk (source_ref_id, text) VALUES (%s, %s)",
                        (ev_src, f"{spec.name}: {e.narrative} {e.outcome}."),
                    )

        conn.execute(
            "INSERT INTO audit_log (actor, action, entity, payload) VALUES ('seed', 'seed',"
            " 'well', %s)",
            (Jsonb({"wells": [w.name for w in WELLS]}),),
        )
    print(f"seeded {len(FORMATIONS)} formations, {len(WELLS)} wells")


if __name__ == "__main__":
    main()
