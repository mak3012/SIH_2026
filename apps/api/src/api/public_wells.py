"""Public wells (Sodir FactPages, synced daily by the `public_wells` Dagster asset).

GET /public-wells          every public wellbore the caller may see (block NO-SODIR), for the map
GET /public-wells/status   last sync, totals, wells new in the last 30 days
"""

from typing import Any

import psycopg
from fastapi import APIRouter

from api.intelligence.routes import Conn

router = APIRouter(tags=["public wells"])
NEW_DAYS = 30

COLUMNS = [
    "id", "name", "source", "block", "lat", "lon", "area", "purpose", "status", "content",
    "operator", "field", "entry_date", "completion_date", "total_depth_m", "water_depth_m",
    "formation_at_td", "factpage_url", "press_release_url", "first_seen", "is_new", "reports",
]  # fmt: skip


@router.get("/public-wells")
def public_wells(conn: psycopg.Connection = Conn) -> list[dict[str, Any]]:
    """New = first seen by the sync, or spudded, in the last 30 days (not the first sync's
    whole register: that would mark every well new)."""
    rows = conn.execute(
        "WITH first AS (SELECT min(first_seen) AS t FROM public_well)"
        " SELECT p.id, p.name, p.source, p.block_id, ST_Y(p.surface), ST_X(p.surface), p.area,"
        " p.purpose, p.status, p.content, p.operator, p.field, p.entry_date, p.completion_date,"
        " p.total_depth_m, p.water_depth_m, p.formation_at_td, p.factpage_url,"
        " p.press_release_url, p.first_seen,"
        " (p.first_seen > (SELECT t FROM first) + interval '1 hour'"
        "   AND p.first_seen > now() - make_interval(days => %(d)s))"
        "  OR p.entry_date > current_date - %(d)s,"
        " (SELECT count(*) FROM document d WHERE d.source = p.source"
        "   AND d.title LIKE p.name || ' %%')"
        " FROM public_well p ORDER BY p.entry_date DESC NULLS LAST, p.id",
        {"d": NEW_DAYS},
    ).fetchall()
    return [dict(zip(COLUMNS, r, strict=True)) for r in rows]


@router.get("/public-wells/status")
def public_wells_status(conn: psycopg.Connection = Conn) -> dict[str, Any]:
    total, synced, newest = conn.execute(
        "SELECT count(*), max(synced_at), max(entry_date) FROM public_well"
    ).fetchone()  # type: ignore[misc]
    return {"wells": total, "last_sync": synced, "newest_spud": newest, "new_days": NEW_DAYS}
