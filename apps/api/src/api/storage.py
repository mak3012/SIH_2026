"""Object storage for source files: read documents / audio by URI, write uploaded voice notes.

s3://bucket/key        the S3 object store (S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY)
synthetic://well/title synthetic records loaded without files (load_truth); served from
                       SYNTHETIC_RAW (data/raw) when the generator's files are there, else missing
"""

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit


class Missing(LookupError):
    """No stored file for this URI."""


def _client() -> Any:
    from minio import Minio

    endpoint = urlsplit(os.environ.get("S3_ENDPOINT", "https://s3:9000"))
    return Minio(
        endpoint.netloc,
        os.environ.get("S3_ACCESS_KEY"),
        os.environ.get("S3_SECRET_KEY"),
        secure=endpoint.scheme == "https",
    )


@lru_cache(maxsize=1)
def _synthetic_index() -> dict[str, Path]:
    """title -> file, from the generator's documents.json (written next to the files)."""
    raw = os.environ.get("SYNTHETIC_RAW")
    if not raw:
        return {}
    root = Path(raw)
    out: dict[str, Path] = {}
    for f in root.glob("*/documents.json"):
        for d in json.loads(f.read_text(encoding="utf-8")).get("documents", []):
            out[d["title"]] = root / d["path"]
    return out


@lru_cache(maxsize=32)
def read(uri: str) -> bytes:
    """The bytes behind a document or audio URI. Raises Missing when nothing is stored."""
    u = urlsplit(uri)
    if u.scheme == "s3":
        obj = _client().get_object(u.netloc, u.path.lstrip("/"))
        try:
            return bytes(obj.read())
        finally:
            obj.close()
            obj.release_conn()
    if u.scheme == "synthetic":
        rel = unquote(u.path.lstrip("/"))
        path = _synthetic_index().get(rel)
        raw = os.environ.get("SYNTHETIC_RAW")
        if path is None and raw and rel.startswith("voice/"):  # voice/<event>.wav
            path = Path(raw) / "synthetic_assam" / "wells" / u.netloc / "audio" / rel[6:]
        if path is None or not path.is_file():
            raise Missing(uri)
        return path.read_bytes()
    raise Missing(uri)


def put(bucket: str, key: str, data: bytes, content_type: str) -> str:
    """Store an upload; returns its s3:// URI."""
    import io

    client = _client()
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
    client.put_object(bucket, key, io.BytesIO(data), len(data), content_type=content_type)
    return f"s3://{bucket}/{key}"
