"""Database access under row-level security. See docs/data-model.md#access-control."""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager

import psycopg

ROLES = ("app_rw", "app_ro")


@contextmanager
def scoped(
    conn: psycopg.Connection, role: str, blocks: Iterable[str], jvs: Iterable[str] = ()
) -> Iterator[psycopg.Connection]:
    """One transaction as `role`, with ABAC attributes from the caller's token.

    RLS then filters every statement. app_ro (chat text-to-SQL) is also read-only and time-boxed.
    """
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    blocks, jvs = list(blocks), list(jvs)
    if any("," in v for v in blocks + jvs):
        raise ValueError("block/JV ids must not contain commas")
    # SET LOCAL lasts to the end of the top-level transaction, not a savepoint: nesting would
    # leak the role and attributes into the caller's transaction.
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError("scoped() must start its own top-level transaction")
    with conn.transaction():
        if role == "app_ro":
            conn.execute("SET LOCAL transaction_read_only = on")
            conn.execute("SET LOCAL statement_timeout = '5s'")
        conn.execute(f"SET LOCAL ROLE {role}")
        conn.execute(
            "SELECT set_config('app.blocks', %s, true), set_config('app.jvs', %s, true)",
            (",".join(blocks), ",".join(jvs)),
        )
        yield conn
