"""Load every document and audio note listed in data/raw/*/register.json into the S3 object store
and Postgres. Idempotent: objects already stored with the same sha256 are not re-uploaded, and
rows are keyed on sha256 (ON CONFLICT DO NOTHING).

Usage: python register.py      (needs DATABASE_URL, S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY)

Runs as the table owner, like any trusted ingestion (bypasses row-level security).
"""

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psycopg
import pypdfium2 as pdfium

from fetch import RAW, sha256_of

BUCKETS = {"document": "documents", "audio": "audio"}
Put = Callable[[str, str, Path, str], bool]  # (bucket, key, file, sha256) -> uploaded?


def load_items(raw: Path = RAW) -> list[dict[str, Any]]:
    items = []
    for reg in sorted(raw.glob("*/register.json")):
        items += json.loads(reg.read_text())
    return items


def minio_put() -> Put:
    from minio import Minio
    from minio.error import S3Error

    url = urlsplit(os.environ["S3_ENDPOINT"])
    client = Minio(
        url.netloc,
        os.environ["S3_ACCESS_KEY"],
        os.environ["S3_SECRET_KEY"],
        secure=url.scheme == "https",
    )
    for bucket in BUCKETS.values():
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)

    def put(bucket: str, key: str, path: Path, sha256: str) -> bool:
        try:
            meta = client.stat_object(bucket, key).metadata or {}
            if meta.get("x-amz-meta-sha256") == sha256:
                return False
        except S3Error as e:
            if e.code not in ("NoSuchKey", "NoSuchObject"):
                raise
        client.fput_object(bucket, key, str(path), metadata={"sha256": sha256})
        return True

    return put


def register(
    items: list[dict[str, Any]], conn: psycopg.Connection, put: Put, raw: Path = RAW
) -> dict[str, int]:
    counts = {"uploaded": 0, "rows": 0, "existing": 0, "missing": 0}
    for it in items:
        path = raw / it["path"]
        if not path.exists():
            print(f"  missing {it['path']} (listed but not on disk)")
            counts["missing"] += 1
            continue
        sha = sha256_of(path)
        bucket = BUCKETS[it["kind"]]
        counts["uploaded"] += put(bucket, it["path"], path, sha)
        uri = f"s3://{bucket}/{it['path']}"
        conn.execute(
            "INSERT INTO block (id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (it["block_id"], it["block_name"]),
        )
        if it["kind"] == "document":
            pages = len(pdfium.PdfDocument(str(path))) if path.suffix.lower() == ".pdf" else None
            cur = conn.execute(
                "INSERT INTO document (sha256, type, title, vintage, classification, uri,"
                " page_count, block_id, source, synthetic)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (sha256) DO NOTHING",
                (
                    bytes.fromhex(sha),
                    it["doc_type"],
                    it["title"],
                    it.get("vintage"),
                    it["classification"],
                    uri,
                    pages,
                    it["block_id"],
                    it["source"],
                    it["synthetic"],
                ),
            )
        else:
            cur = conn.execute(
                "INSERT INTO audio_note (sha256, uri, duration_s, speaker, lang, block_id,"
                " source, synthetic) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (sha256) DO NOTHING",
                (
                    bytes.fromhex(sha),
                    uri,
                    it["duration_s"],
                    it.get("speaker"),
                    it["lang"],
                    it["block_id"],
                    it["source"],
                    it["synthetic"],
                ),
            )
        counts["rows" if cur.rowcount else "existing"] += 1
    return counts


def main() -> None:
    items = load_items()
    print(f"register: {len(items)} items from {len(list(RAW.glob('*/register.json')))} sources")
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        counts = register(items, conn, minio_put())
    print(
        f"  {counts['uploaded']} uploaded to the object store, {counts['rows']} new rows, "
        f"{counts['existing']} already registered, {counts['missing']} missing"
    )


if __name__ == "__main__":
    main()
