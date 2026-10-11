"""Registration into Postgres (real DB via TEST_DATABASE_URL) with a fake object store."""

import math
import os
import struct
import wave
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from register import register
from synthetic_assam import render

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
INI = Path(__file__).resolve().parents[2] / "apps" / "api" / "alembic.ini"


@pytest.fixture
def raw(tmp_path: Path) -> Path:
    doc = render.TypedDoc("t", "SYNTHETIC")
    doc.text(20, 20, "hello")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.pdf").write_bytes(doc.pdf())
    with wave.open(str(tmp_path / "src" / "a.wav"), "wb") as f:
        f.setnchannels(1), f.setsampwidth(2), f.setframerate(8000)
        f.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(i))) for i in range(800)))
    return tmp_path


def test_register_is_idempotent_and_flags_synthetic(raw: Path) -> None:
    assert URL
    cfg = Config(str(INI))
    cfg.set_main_option("sqlalchemy.url", URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    common = {
        "source": "synthetic_assam",
        "synthetic": True,
        "classification": "internal",
        "block_id": "SA-BLK-9",
        "block_name": "SA-BLK-9 (synthetic)",
        "title": "t",
    }
    items = [
        {
            "path": "src/a.pdf",
            "kind": "document",
            "doc_type": "ddr",
            "vintage": "1990-01-02",
            **common,
        },
        {
            "path": "src/a.wav",
            "kind": "audio",
            "duration_s": 0.1,
            "speaker": "x",
            "lang": "en",
            **common,
        },
        {"path": "src/gone.pdf", "kind": "document", "doc_type": "ddr", **common},
    ]
    stored: dict[str, str] = {}

    def put(bucket: str, key: str, path: Path, sha: str) -> bool:
        new = stored.get(f"{bucket}/{key}") != sha
        stored[f"{bucket}/{key}"] = sha
        return new

    with psycopg.connect(URL) as conn:
        first = register(items, conn, put, raw)
        second = register(items, conn, put, raw)
        row = conn.execute(
            "SELECT uri, page_count, synthetic, source FROM document WHERE title = 't'"
        ).fetchone()
        audio = conn.execute("SELECT synthetic, uri FROM audio_note").fetchall()
    assert first == {"uploaded": 2, "rows": 2, "existing": 0, "missing": 1}
    assert second == {"uploaded": 0, "rows": 0, "existing": 2, "missing": 1}
    assert row == ("s3://documents/src/a.pdf", 1, True, "synthetic_assam")
    assert audio == [(True, "s3://audio/src/a.wav")]
    command.downgrade(cfg, "base")  # leave the database as other suites expect to find it
    command.upgrade(cfg, "head")
