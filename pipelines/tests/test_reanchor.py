"""A geologist picks a top; the well's formation-relative hazards move with it (real database)."""

from math import isclose

import psycopg

from core.canonical.survey import Station, md_at_tvd, minimum_curvature
from pipelines.reanchor import pick_top, stale_wellbores


def test_pick_top_reprojects_hazards(conn: psycopg.Connection) -> None:
    c = conn
    c.execute("INSERT INTO basin VALUES ('UA', 'Upper Assam', NULL) ON CONFLICT DO NOTHING")
    c.execute("INSERT INTO block VALUES ('B-R', 'r', 'x') ON CONFLICT DO NOTHING")
    fid = {n: c.execute("INSERT INTO formation (basin, name) VALUES ('UA', %s) RETURNING id",
                        (n,)).fetchone()[0]  # type: ignore[index]
           for n in ("Tipam", "Barail", "Kopili")}  # fmt: skip
    doc = c.execute(
        "INSERT INTO document (sha256, type, title, uri, block_id) VALUES"
        " (sha256('reanchor'::bytea), 'wcr', 't', 's3://x', 'B-R') RETURNING id"
    ).fetchone()[0]  # type: ignore[index]
    src = c.execute(
        "INSERT INTO source_ref (document_id, extractor_version) VALUES (%s, 't') RETURNING id",
        (doc,),
    ).fetchone()[0]  # type: ignore[index]
    well = c.execute(
        "INSERT INTO well (name, block_id, surface, rt_elevation_m) VALUES"
        " ('R-1', 'B-R', ST_SetSRID(ST_MakePoint(95, 27), 4326), 110) RETURNING id"
    ).fetchone()[0]  # type: ignore[index]
    wb = c.execute(
        "INSERT INTO wellbore (well_id, name, datum_type, datum_elevation_m)"
        " VALUES (%s, 'R-1', 'KB', 112) RETURNING id",
        (well,),
    ).fetchone()[0]  # type: ignore[index]
    stations = [Station(0, 0, 0), Station(1000, 0, 0), Station(4200, 30, 45)]
    for s, p in zip(stations, minimum_curvature(stations), strict=True):
        c.execute(
            "INSERT INTO survey_station (wellbore_id, md, inc, azi, tvd, tvdss, geom,"
            " source_ref_id) VALUES (%s, %s, %s, %s, %s, %s,"
            " ST_SetSRID(ST_MakePoint(95, 27, 0), 4326), %s)",
            (wb, s.md, s.inc, s.azi, p.tvd, p.tvd - 112, src),
        )
    for name, tvdss in (("Tipam", 2050.0), ("Barail", 2760.0), ("Kopili", 3480.0)):
        c.execute(
            "INSERT INTO formation_top (wellbore_id, formation_id, md, tvdss, source_ref_id)"
            " VALUES (%s, %s, 0, %s, %s)",
            (wb, fid[name], tvdss, src),
        )
    hz = c.execute(
        "INSERT INTO hazard_profile (wellbore_id, hazard, formation_id, tvdss_top, tvdss_base,"
        " prior, model_version, rel_top, rel_base) VALUES (%s, 'loss', %s, 2760, 2832, 0.3,"
        " 't', 0.0, 0.1) RETURNING id",
        (wb, fid["Barail"]),
    ).fetchone()[0]  # type: ignore[index]

    new_md = md_at_tvd(stations, 2800.0 + 112)  # the geologist picks the Barail 40 m deeper
    out = pick_top(c, wb, fid["Barail"], new_md, "geologist.a")
    assert isclose(out["tvdss"], 2800.0, abs_tol=1e-4) and out["note"] is None
    top, base, md_top = c.execute(
        "SELECT tvdss_top, tvdss_base, md_top FROM hazard_profile WHERE id = %s", (hz,)
    ).fetchone()  # type: ignore[misc]
    assert isclose(top, 2800.0, abs_tol=0.01) and isclose(base, 2800 + 0.1 * 680, abs_tol=0.01)
    assert isclose(md_top, new_md, abs_tol=0.01)
    pick = c.execute(
        "SELECT r.reviewer, r.review_status, d.title FROM formation_top t"
        " JOIN source_ref r ON r.id = t.source_ref_id JOIN document d ON d.id = r.document_id"
        " WHERE r.extractor_version = 'human'"
    ).fetchone()
    assert pick == ("geologist.a", "accepted", "Top picks, well R-1")
    assert c.execute("SELECT count(*) FROM audit_log WHERE action = 'pick_top'").fetchone() == (1,)
    assert stale_wellbores(c) == []
