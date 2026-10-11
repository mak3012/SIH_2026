import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"
GOLD = RAW / "synthetic_assam" / "gold" / "gold_set.json"
URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def gold_path() -> Path:
    if not GOLD.exists():
        pytest.skip("gold set not generated: cd data && python -m synthetic_assam --no-audio")
    return GOLD


@pytest.fixture(scope="module")
def db_url() -> str:
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    cfg = Config(str(ROOT / "apps" / "api" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    return URL


@pytest.fixture
def conn(db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(db_url, autocommit=True) as c:
        yield c
