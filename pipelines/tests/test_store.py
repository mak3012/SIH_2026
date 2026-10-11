"""Storage against a real database (TEST_DATABASE_URL): accepted facts carry a source_ref with
page + bbox; everything below threshold lands in review_item; reruns store nothing twice."""

import json
import tomllib
from pathlib import Path
from typing import Any

import psycopg
import pytest

from pipelines.process import Settings, process
from pipelines.store import store_document

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"


def _doc(conn: psycopg.Connection, path: str, doc_type: str) -> dict[str, Any]:
    conn.execute("INSERT INTO block VALUES ('SA-T', 'test', 'x') ON CONFLICT DO NOTHING")
    row = conn.execute(
        "INSERT INTO document (sha256, type, title, uri, block_id, synthetic)"
        " VALUES (sha256(%s::bytea), %s, %s, %s, 'SA-T', true) RETURNING id",
        (path.encode(), doc_type, path, f"s3://documents/{path}"),
    ).fetchone()
    assert row is not None
    return {"id": row[0], "block_id": "SA-T", "jv_id": None}


@pytest.fixture
def typed_well(gold_path: Path, conn: psycopg.Connection) -> dict[str, str]:
    """A synthetic well whose WCR and survey report are both typed; formations registered."""
    docs = json.loads((RAW / "synthetic_assam" / "documents.json").read_text())["documents"]
    wells: dict[str, dict[str, str]] = {}
    for d in docs:
        if d["render"] == "typed":
            wells.setdefault(d["well"], {})[d["doc_type"]] = d["path"]
    well = next(w for w, t in sorted(wells.items()) if {"wcr", "survey_report", "ddr"} <= t.keys())
    cfg = tomllib.loads((ROOT / "data" / "synthetic_assam" / "stratigraphy.toml").read_text())
    conn.execute(
        "INSERT INTO basin (id, name) VALUES ('Upper Assam', 'Upper Assam') ON CONFLICT DO NOTHING"
    )
    for f in cfg["formation"]:
        conn.execute(
            "INSERT INTO formation (basin, name, synonyms) VALUES ('Upper Assam', %s, %s)"
            " ON CONFLICT DO NOTHING",
            (f["name"], f["synonyms"]),
        )
    return {"name": well, **wells[well]}


def test_accepted_facts_have_provenance_and_reruns_are_idempotent(
    conn: psycopg.Connection, typed_well: dict[str, str], tmp_path: Path
) -> None:
    # single_pass = 1.0 stands in for an agreeing second pass, so typed values are accepted.
    s = Settings(engines=("text_layer",), single_pass=1.0)
    wcr = process(RAW / typed_well["wcr"], settings=s, cache=tmp_path / "w")
    doc = _doc(conn, typed_well["wcr"], "wcr")
    counts = store_document(conn, doc, wcr, s.threshold)
    assert counts["facts"] > 10
    tops = conn.execute(
        "SELECT count(*) FROM formation_top t JOIN source_ref r ON r.id = t.source_ref_id"
        " WHERE r.document_id = %s AND r.page IS NOT NULL AND cardinality(r.bbox) = 4",
        (doc["id"],),
    ).fetchone()
    assert tops == (sum(1 for n in wcr.fields if n.endswith(".formation") and "tops" in n),)
    well = conn.execute(
        "SELECT w.datum_confidence, w.position_uncertainty_m, b.datum_type, b.datum_elevation_m"
        " FROM well w JOIN wellbore b ON b.well_id = w.id WHERE w.name = %s",
        (typed_well["name"],),
    ).fetchone()
    assert well is not None and well[0] == "known" and well[1] in (0.0, 22.0)
    assert well[2] in ("KB", "RT") and well[3] is not None
    matches = conn.execute(
        "SELECT status, proposed_by FROM name_match WHERE document_id = %s", (doc["id"],)
    ).fetchall()
    assert matches and all(m == ("confirmed", "rule") for m in matches)
    if wcr.depth_unit == "ft":  # every converted depth carries its conversion log
        logs = conn.execute(
            "SELECT conversions FROM source_ref WHERE document_id = %s AND conversions <> '[]'",
            (doc["id"],),
        ).fetchall()
        assert logs and logs[0][0][0]["unit_in"] == "ft"
    assert store_document(conn, doc, wcr, s.threshold) == {"facts": 0, "review": 0, "skipped": 1}

    survey = process(RAW / typed_well["survey_report"], settings=s, cache=tmp_path / "s")
    sdoc = _doc(conn, typed_well["survey_report"], "survey_report")
    store_document(conn, sdoc, survey, s.threshold)
    n = conn.execute(
        "SELECT count(*) FROM survey_station s JOIN source_ref r ON r.id = s.source_ref_id"
        " WHERE r.document_id = %s",
        (sdoc["id"],),
    ).fetchone()
    assert n is not None and n[0] == sum(1 for k in survey.fields if k.endswith("].md"))


def test_everything_below_threshold_goes_to_review(
    conn: psycopg.Connection, typed_well: dict[str, str], tmp_path: Path
) -> None:
    # Default settings and no second pass: consistency 0.5, so nothing is auto-accepted.
    s = Settings(engines=("text_layer",))
    ddr = process(RAW / typed_well["ddr"], settings=s, cache=tmp_path / "d")
    doc = _doc(conn, typed_well["ddr"] + "#review", "ddr")
    store_document(conn, doc, ddr, s.threshold)
    low = {n for n, f in ddr.fields.items() if f.confidence < s.threshold}
    queued = {
        r[0]
        for r in conn.execute(
            "SELECT field FROM review_item WHERE document_id = %s AND kind = 'field'", (doc["id"],)
        )
    }
    assert low and queued == low
    facts = conn.execute(
        "SELECT count(*) FROM source_ref WHERE document_id = %s", (doc["id"],)
    ).fetchone()
    assert facts == (0,)
