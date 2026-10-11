"""Seed data for the UI tests and screenshots, captured from the real API.

Every JSON file in this folder is a response of the API (DEV_AUTH, engineer + admin, all blocks)
over the synthetic Upper Assam dataset after a leave-one-well-out replay. Nothing is typed by
hand, so the screens are tested against the shapes and values the API really returns.

    export DATABASE_URL=postgresql://... SYNTHETIC_RAW=data/raw
    uv run python tests/e2e/fixtures/capture.py extract
    uv run python tests/e2e/fixtures/capture.py capture

The database needs: the synthetic truth loaded (data/synthetic_assam load_truth), a replay
(`python -m api.replay evaluate`, which leaves real-time samples and alerts for every well), and
for `extract`, the OCR engines of the pipelines package (the review queue comes from a real
extraction of three hard documents). `extract` writes to the database; run it once.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
RAW = Path(os.environ.get("SYNTHETIC_RAW", ROOT / "data" / "raw"))
ACTIVE = "SA-19"  # the "active" well of the screens
TARGET = "Barail"
EXTRACT = [
    "SA-19 WCR (SYNTHETIC)",
    "SA-19 field note SA-19-E1 (SYNTHETIC)",
    "SA-07 WCR (SYNTHETIC)",
]
QUESTIONS = [
    "What is the Barail top in SA-19?",
    "Which wells had losses in the Barail?",
    "What drilling problems were recorded in SA-07?",
]


def extract() -> None:
    from pipelines.process import Settings, process
    from pipelines.store import store_document

    docs = {
        d["title"]: d
        for d in json.loads((RAW / "synthetic_assam" / "documents.json").read_text())["documents"]
    }
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        for title in EXTRACT:
            row = conn.execute(
                "SELECT id, block_id, jv_id FROM document WHERE title = %s", (title,)
            ).fetchone()
            assert row, title
            s = Settings.from_env()
            res = process(RAW / docs[title]["path"], settings=s, cache=RAW / "eval" / "ocr-cache")
            doc = {"id": row[0], "block_id": row[1], "jv_id": row[2]}
            print(title, store_document(conn, doc, res, s.threshold), flush=True)


def capture() -> None:
    os.environ["DEV_AUTH"] = "1"
    os.environ.setdefault("LLM_BACKEND", "none")
    from fastapi.testclient import TestClient

    from api.main import app

    c = TestClient(app)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        blocks = ",".join(r[0] for r in conn.execute("SELECT id FROM block ORDER BY id"))
    roles = "wellsite_engineer,planner,data_steward,admin"
    h = {"X-User": "a.engineer", "X-Roles": roles, "X-Blocks": blocks}

    def get(path: str, **params: Any) -> Any:
        r = c.get(path, params=params, headers=h)
        assert r.status_code == 200, (path, r.status_code, r.text[:300])
        return r.json()

    def save(name: str, data: Any) -> None:
        (HERE / f"{name}.json").write_text(json.dumps(data, indent=1, default=str) + "\n")
        print("wrote", name, flush=True)

    wells = get("/wellbores")
    save("wellbores", wells)
    bore = next(w["wellbore_id"] for w in wells if w["well"] == ACTIVE)
    sel = c.post(
        "/offsets/select",
        json={
            "active_wellbore_id": bore,
            "formation": TARGET,
            "surface_radius_m": 30000,
            "subsurface_radius_m": 25000,
        },
        headers=h,
    )
    assert sel.status_code == 200, sel.text
    save("offsets-select", sel.json())
    save("formations", get(f"/wellbores/{bore}/formations"))
    save("hazard-profile", get(f"/active/{bore}/hazard-profile"))
    save("lookahead", get(f"/active/{bore}/lookahead"))
    save("alerts", get(f"/active/{bore}/alerts"))
    save("scene", get(f"/wellbores/{bore}/scene"))
    save(
        "offsets-preview",
        get(
            f"/offsets/preview/{bore}",
            formation=TARGET,
            surface_radius_m=30000,
            subsurface_radius_m=25000,
        ),
    )
    save("correlation", get(f"/correlation/{bore}"))
    save("history", get(f"/active/{bore}/history", minutes=90, max_points=900))
    save("envelope", get(f"/active/{bore}/envelope"))
    save("fleet", get("/fleet"))
    save("admin-status", get("/admin/status"))
    asks = []
    for q in QUESTIONS:
        r = c.post("/assistant/ask", json={"question": q}, headers=h)
        assert r.status_code == 200, r.text
        asks.append(r.json())
    save("ask", asks)
    forms = get(f"/wellbores/{bore}/formations")
    tops = [f for f in forms if f["top_md"] is not None]
    td = next(w["td_md"] for w in wells if w["wellbore_id"] == bore)
    program = {
        "wellbore_id": bore,
        "td_md": td,
        "ecd_margin_sg": 0.05,
        "sections": [
            {
                "name": "17-1/2 in surface",
                "md_top": 0,
                "md_base": 500,
                "hole_in": 17.5,
                "casing_od_in": 13.375,
                "mud_type": "wbm",
                "mw_sg_min": 1.05,
                "mw_sg_max": 1.1,
            },
            {
                "name": "12-1/4 in intermediate",
                "md_top": 500,
                "md_base": round(tops[-2]["top_md"]),
                "hole_in": 12.25,
                "casing_od_in": 9.625,
                "mud_type": "wbm",
                "mw_sg_min": 1.1,
                "mw_sg_max": 1.2,
            },
            {
                "name": "8-1/2 in production",
                "md_top": round(tops[-2]["top_md"]),
                "md_base": td,
                "hole_in": 8.5,
                "casing_od_in": 7,
                "mud_type": "wbm",
                "mw_sg_min": 1.15,
                "mw_sg_max": 1.3,
            },
        ],
        "cement": [],
    }
    save("program", program)
    lint = c.post("/programs/lint", json={"program": program}, headers=h)
    assert lint.status_code == 200, lint.text
    save("lint", lint.json())
    brief = c.post(
        f"/briefings/{bore}", json={"lint_run_id": lint.json()["lint_run_id"]}, headers=h
    )
    assert brief.status_code == 200, brief.text
    save("briefing", brief.json())
    pdf = c.get(brief.json()["pdf_url"], headers=h)
    (HERE / "briefing.pdf").write_bytes(pdf.content)
    review = get("/review/items", status="all", limit=200)
    save("review", review)
    doc = next(r["document_id"] for r in review if r["document_id"] and r["title"] == EXTRACT[0])
    save("document-fields", get(f"/documents/{doc}/fields"))
    pdf = c.get(f"/documents/{doc}/file", headers=h)
    assert pdf.status_code == 200, pdf.text
    (HERE / "document.pdf").write_bytes(pdf.content)


if __name__ == "__main__":
    {"extract": extract, "capture": capture}[sys.argv[1]]()
