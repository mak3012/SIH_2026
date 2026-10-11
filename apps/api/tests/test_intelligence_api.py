"""The intelligence endpoints end to end, against a database (TEST_DATABASE_URL): demo seed
plus a planned well, called the way a client calls them, with row-level security in force."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from core.canonical.survey import Station, minimum_curvature

B1, B2 = "AA-ONN-2001/1", "AA-ONN-2001/2"


def headers(
    roles: str = "planner,wellsite_engineer,data_steward", blocks: str = f"{B1},{B2}"
) -> dict[str, str]:
    return {"X-User": "tester", "X-Roles": roles, "X-Blocks": blocks}


@pytest.fixture(scope="module")
def world(db_url: str) -> Iterator[dict[str, Any]]:
    mp = pytest.MonkeyPatch()
    mp.setenv("DATABASE_URL", db_url)
    mp.setenv("DEV_AUTH", "1")
    mp.setenv("LLM_BACKEND", "none")
    from api import seed

    seed.main()
    with psycopg.connect(db_url) as c:
        doc = c.execute(
            "INSERT INTO document (sha256, type, title, uri, block_id) VALUES"
            " (sha256('plan-1'::bytea), 'other', 'PLAN-1 well plan', 's3://plans/1', %s)"
            " RETURNING id",
            (B1,),
        ).fetchone()[0]  # type: ignore[index]
        src = c.execute(
            "INSERT INTO source_ref (document_id, extractor_version) VALUES (%s,"
            " 'plan') RETURNING id",
            (doc,),
        ).fetchone()[0]  # type: ignore[index]
        well = c.execute(
            "INSERT INTO well (name, block_id, surface, rt_elevation_m, fault_block, spud_date)"
            " VALUES ('PLAN-1', %s, ST_SetSRID(ST_MakePoint(94.905, 27.105), 4326), 115,"
            " 'FB-North', '2026-10-01') RETURNING id",
            (B1,),
        ).fetchone()[0]  # type: ignore[index]
        bore = c.execute(
            "INSERT INTO wellbore (well_id, name, status, td_md, datum_type, datum_elevation_m)"
            " VALUES (%s, 'PLAN-1', 'planned', 3700, 'RT', 115) RETURNING id",
            (well,),
        ).fetchone()[0]  # type: ignore[index]
        stations = [Station(0, 0, 0), Station(1000, 0, 0), Station(3700, 20, 60)]
        for s, p in zip(stations, minimum_curvature(stations), strict=True):
            c.execute(
                "INSERT INTO survey_station (wellbore_id, md, inc, azi, tvd, tvdss, geom,"
                " source_ref_id) VALUES (%s, %s, %s, %s, %s, %s,"
                " ST_SetSRID(ST_MakePoint(94.905, 27.105, %s), 4326), %s)",
                (bore, s.md, s.inc, s.azi, p.tvd, p.tvd - 115, -(p.tvd - 115), src),
            )
        fid = dict(c.execute("SELECT name, id FROM formation").fetchall())
        for name, tvdss in (("Tipam", 1760.0), ("Barail", 2630.0), ("Kopili", 3280.0)):
            c.execute(
                "INSERT INTO formation_top (wellbore_id, formation_id, md, tvdss, source_ref_id)"
                " VALUES (%s, %s, %s, %s, %s)",
                (bore, fid[name], tvdss + 115, tvdss, src),
            )
        now = datetime(2026, 10, 1, 12, tzinfo=UTC)
        for h in range(13):  # 13 h of drilling at 12 m/h, bit now at 1,944 m
            c.execute(
                "INSERT INTO rt_measurement (wellbore_id, ts, hole_md, bit_md)"
                " VALUES (%s, %s, %s, %s)",
                (bore, now - timedelta(hours=12 - h), 1800 + 12 * h, 1800 + 12 * h),
            )
        wells = dict(c.execute("SELECT name, id FROM well").fetchall())
    from api.main import app

    yield {"client": TestClient(app), "bore": bore, "wells": wells}
    mp.undo()


def test_wells_near(world: dict[str, Any]) -> None:
    r = world["client"].get(
        "/wells", params={"near": "well:DEMO-1", "radius_m": 5000}, headers=headers()
    )
    names = [w["well"] for w in r.json()]
    assert r.status_code == 200 and names[0] == "DEMO-1" and {"DEMO-2", "DEMO-3"} <= set(names)
    assert "DEMO-5" not in names and r.json()[1]["distance_m"] > 0
    # Row-level security: a user without block /1 cannot even find DEMO-1.
    r = world["client"].get("/wells", params={"near": "well:DEMO-1"}, headers=headers(blocks=B2))
    assert r.status_code == 404


def select(world: dict[str, Any], **over: Any) -> Any:
    body = {
        "active_wellbore_id": world["bore"],
        "formation": "BARAIL",
        "surface_radius_m": 20000,
        "subsurface_radius_m": 15000,
        "excluded": [world["wells"]["DEMO-5"]],
        **over,
    }
    return world["client"].post("/offsets/select", json=body, headers=headers())


def test_offsets_select_scores_and_stores(world: dict[str, Any]) -> None:
    r = select(world, weights={"vintage": 0.0})
    assert r.status_code == 200, r.text
    body = r.json()
    by = {c["well"]: c for c in body["candidates"]}
    assert set(by) == {"DEMO-1", "DEMO-2", "DEMO-3", "DEMO-4", "DEMO-5"}
    assert by["DEMO-5"]["excluded"] and not by["DEMO-5"]["included"]
    assert by["DEMO-1"]["included"] and by["DEMO-1"]["formation_m"] is not None
    f = {x["name"]: x for x in by["DEMO-1"]["factors"]}
    assert f["fault_block"]["score"] == 1.0 and f["vintage"]["weight"] == 0.0
    assert {x["name"]: x for x in by["DEMO-4"]["factors"]}["fault_block"]["score"] == 0.0
    assert abs(sum(x["contribution"] for x in f.values()) - by["DEMO-1"]["score"]) < 2e-3
    assert body["candidates"] == sorted(body["candidates"], key=lambda c: c["rank"])
    # Bad requests are refused, and only planners select offsets.
    assert select(world, weights={"shoe_size": 1}).status_code == 422
    r = world["client"].post(
        "/offsets/select",
        json={"active_wellbore_id": world["bore"], "formation": "Barail"},
        headers=headers(roles="wellsite_engineer,data_steward,admin"),
    )
    assert r.status_code == 403


def test_hazard_profile_and_lookahead(world: dict[str, Any]) -> None:
    assert select(world).status_code == 200
    r = world["client"].get(f"/active/{world['bore']}/hazard-profile", headers=headers())
    assert r.status_code == 200, r.text
    cells = {(c["formation"], c["hazard"]): c for c in r.json()["cells"]}
    loss_b, loss_k = cells[("Barail", "loss")], cells[("Kopili", "loss")]
    n_offsets = len(r.json()["offsets"])
    assert sum(loss_b["counts"].values()) == n_offsets == 4
    assert loss_b["counts"]["event"] >= 2 and loss_b["mean"] > loss_k["mean"]
    assert loss_b["ci90"][0] < loss_b["mean"] < loss_b["ci90"][1]
    assert loss_b["band_md"] is not None and loss_b["evidence"][0]["document"]
    r = world["client"].get(f"/active/{world['bore']}/lookahead", headers=headers())
    la = r.json()
    assert la["current_md"] == 1944.0 and abs(la["rate_m_per_hr"] - 12.0) < 1e-6
    ahead = [b for b in la["bands"] if b["state"] == "ahead"]
    assert ahead and all(abs(b["eta_hours"] - b["distance_m"] / 12.0) < 0.06 for b in ahead)


PROGRAM = {
    "wellbore_id": 0,
    "td_md": 3700.0,
    "ecd_margin_sg": 0.05,
    "sections": [
        {
            "name": "17-1/2 in surface",
            "md_top": 0,
            "md_base": 520,
            "hole_in": 17.5,
            "casing_od_in": 13.375,
            "mud_type": "wbm",
            "mw_sg_min": 1.05,
            "mw_sg_max": 1.10,
        },
        {
            "name": "12-1/4 in intermediate",
            "md_top": 520,
            "md_base": 2700,
            "hole_in": 12.25,
            "casing_od_in": 9.625,
            "mud_type": "wbm",
            "mw_sg_min": 1.15,
            "mw_sg_max": 1.25,
        },
        {
            "name": "8-1/2 in production",
            "md_top": 2700,
            "md_base": 3700,
            "hole_in": 8.5,
            "casing_od_in": 7.0,
            "mud_type": "wbm",
            "mw_sg_min": 1.30,
            "mw_sg_max": 1.55,
        },
    ],
    "cement": [{"casing_od_in": 7.0, "toc_md": 2650.0, "slurry_density_sg": 1.9}],
}


def test_lint_and_briefing_pdf(world: dict[str, Any]) -> None:
    assert select(world).status_code == 200
    prog = {**PROGRAM, "wellbore_id": world["bore"]}
    r = world["client"].post("/programs/lint", json={"program": prog}, headers=headers())
    assert r.status_code == 200, r.text
    lint = r.json()
    rules = {f["rule"] for f in lint["findings"]}
    assert "L-MW-FRAC" in rules and "L-CMT-DENSITY" in rules  # 1.60 ECD / 1.90 slurry vs LOT/FIT
    assert lint["summary"]["block"] >= 2 and lint["evidence"]
    ids = {e["id"] for e in lint["evidence"]}
    assert all(set(f["evidence"]) <= ids for f in lint["findings"])
    r = world["client"].post(
        f"/briefings/{world['bore']}", json={"lint_run_id": lint["lint_run_id"]}, headers=headers()
    )
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["status"] == "fallback" and b["verification"][-1] == []  # no LLM in tests
    pdf = world["client"].get(b["pdf_url"], headers=headers())
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    # Another licence cannot read it.
    assert world["client"].get(b["pdf_url"], headers=headers(blocks=B2)).status_code == 404


def test_wellbore_list_formations_and_cors(world: dict[str, Any]) -> None:
    c = world["client"]
    rows = c.get("/wellbores", headers=headers(blocks=B1)).json()
    names = {r["well"] for r in rows}
    assert "PLAN-1" in names and "DEMO-4" not in names  # DEMO-4 is in the other block
    plan = next(r for r in rows if r["well"] == "PLAN-1")
    assert plan["wellbore_id"] == world["bore"] and plan["open_alerts"] == 0
    forms = c.get(f"/wellbores/{world['bore']}/formations", headers=headers()).json()
    assert [f["formation"] for f in forms][:3] == ["Tipam", "Barail", "Kopili"]
    assert (
        c.get(f"/wellbores/{world['bore']}/formations", headers=headers(blocks=B2)).status_code
        == 404
    )
    pre = c.options(
        "/assistant/ask",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "authorization,content-type"},
    )  # fmt: skip
    assert pre.headers["access-control-allow-origin"] == "http://localhost:5173"
    evil = c.options(
        "/wellbores",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in evil.headers
