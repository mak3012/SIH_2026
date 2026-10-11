"""Database fixtures. DB tests need TEST_DATABASE_URL pointing at a superuser on a
PostGIS + TimescaleDB + pgvector server (CI uses timescale/timescaledb-ha). Without it they skip.

The target database is wiped: every module starts from `downgrade base` + `upgrade head`.
"""

import asyncio
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

URL = os.environ.get("TEST_DATABASE_URL")
if sys.platform == "win32":  # psycopg's async connections need a selector loop
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def alembic_cfg() -> Config:
    assert URL
    cfg = Config(str(INI))
    cfg.set_main_option("sqlalchemy.url", URL)
    return cfg


@pytest.fixture(scope="module")
def db_url() -> str:
    if not URL:
        pytest.skip("TEST_DATABASE_URL not set")
    cfg = alembic_cfg()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    return URL


@pytest.fixture
def cfg(db_url: str) -> Config:
    return alembic_cfg()


@pytest.fixture
def conn(db_url: str) -> Iterator[psycopg.Connection]:
    """Owner connection in autocommit, so each scoped() call is its own transaction."""
    with psycopg.connect(db_url, autocommit=True) as c:
        yield c
