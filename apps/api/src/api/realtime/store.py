"""Idempotent writes of real-time samples, keyed (wellbore, ts, channel).

One row per (wellbore_id, ts) in rt_measurement. A write sets only the channels it carries:
typed columns keep their stored value when the new one is NULL (COALESCE), and the jsonb
`channels` merges key by key. Writing the same sample twice, or the same timestamp from two
connectors with different channels, gives the same row.
"""

from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from api.realtime.channels import CHANNELS, Sample

TYPED = [col for _, col in CHANNELS.values() if col] + ["ecd"]
_COLS = ", ".join(TYPED)
_SET = ", ".join(f"{c} = coalesce(EXCLUDED.{c}, rt_measurement.{c})" for c in TYPED)
UPSERT = (
    f"INSERT INTO rt_measurement (wellbore_id, ts, rig_state, {_COLS}, channels)"
    f" VALUES (%s, %s, %s::rig_state, {', '.join(['%s'] * len(TYPED))}, %s)"
    f" ON CONFLICT (wellbore_id, ts) DO UPDATE SET {_SET},"
    " rig_state = CASE WHEN EXCLUDED.rig_state = 'unknown' THEN rt_measurement.rig_state"
    " ELSE EXCLUDED.rig_state END,"
    " channels = rt_measurement.channels || EXCLUDED.channels"
)


def row(
    s: Sample, rig_state: str = "unknown", derived: dict[str, float] | None = None
) -> tuple[Any, ...]:
    """Parameters for UPSERT. Derived ECD goes to the typed column unless the feed sent one;
    other derived values and untyped channels go to jsonb."""
    v = s.values
    typed = {col: v.get(name) for name, (_, col) in CHANNELS.items() if col}
    extra = {name: v[name] for name, (_, col) in CHANNELS.items() if col is None and name in v}
    d = dict(derived or {})
    typed["ecd"] = v.get("ecd", d.pop("ecd", None))
    extra.update({k: round(float(x), 5) for k, x in d.items()})
    return (s.wellbore_id, s.ts, rig_state, *(typed[c] for c in TYPED), Jsonb(extra))


async def write(conn: AsyncConnection, rows: list[tuple[Any, ...]]) -> None:
    # One execute per row, not executemany: the service shares this connection between tasks
    # (watchdog, explainer, housekeeping), and executemany's pipeline mode fails when another
    # task queries meanwhile ("cannot exit pipeline mode while busy"); execute is serialised.
    for row in rows:
        await conn.execute(UPSERT, row)
