"""Alembic runner. Migrations are hand-written SQL; no ORM metadata to autogenerate from."""

import os

from alembic import context
from sqlalchemy import create_engine

url = context.config.get_main_option("sqlalchemy.url") or os.environ["DATABASE_URL"]
url = url.replace("postgresql://", "postgresql+psycopg://", 1)

with create_engine(url).connect() as connection:
    context.configure(connection=connection, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()
