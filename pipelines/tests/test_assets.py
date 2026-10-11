"""Materialize the document assets end to end with Dagster (database via TEST_DATABASE_URL,
files read straight from data/raw)."""

import json
from pathlib import Path

import psycopg
from dagster import DagsterInstance, materialize

from pipelines.assets import (
    Database,
    Models,
    Storage,
    document_file,
    document_type,
    extraction_passes,
    page_images,
    page_layout,
    stored_facts,
    validated_extraction,
)

RAW = Path(__file__).resolve().parents[2] / "data" / "raw"


def test_document_assets_end_to_end(
    gold_path: Path, db_url: str, conn: psycopg.Connection, tmp_path: Path
) -> None:
    d = next(
        x
        for x in json.loads(gold_path.read_text())["documents"]
        if x["render"] == "typed" and x["doc_type"] == "ddr"
    )
    conn.execute("INSERT INTO block VALUES ('SA-A', 'a', 'x') ON CONFLICT DO NOTHING")
    row = conn.execute(
        "INSERT INTO document (sha256, type, title, uri, block_id, synthetic)"
        " VALUES (sha256(%s::bytea), 'ddr', 't', %s, 'SA-A', true) RETURNING id",
        (d["path"].encode(), f"s3://documents/{d['path']}"),
    ).fetchone()
    assert row is not None
    key = str(row[0])
    instance = DagsterInstance.ephemeral()
    instance.add_dynamic_partitions("document", [key])
    result = materialize(
        [document_file, document_type, page_images, page_layout, extraction_passes,
         validated_extraction, stored_facts],
        partition_key=key,
        instance=instance,
        resources={
            "db": Database(url=db_url),
            "storage": Storage(work_dir=str(tmp_path), local_root=str(RAW)),
            "models": Models(layout_engines="text_layer", second_pass="none"),
        },
    )  # fmt: skip
    assert result.success
    doc = result.output_for_node("validated_extraction")
    assert doc.doc_type == "ddr" and len(doc.fields) >= len(d["fields"]) - 1
    assert (tmp_path / "results" / f"{key}.json").exists()
    n = conn.execute("SELECT count(*) FROM review_item WHERE document_id = %s", (row[0],))
    assert n.fetchone()[0] >= len(doc.fields)  # no second pass: every field waits for review
