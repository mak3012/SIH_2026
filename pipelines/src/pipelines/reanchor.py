"""Live top re-anchoring in the database.

`pick_top` is what a review screen or the API calls when a geologist picks a formation top: in
one transaction it records the pick (with provenance: a per-well "top picks" document, the
geologist as reviewer) and re-projects every formation-relative hazard of that wellbore
(core.canonical.reanchor). `reanchor_wellbore` does only the re-projection; the
`top_picks` sensor runs it for picks inserted any other way.
"""

import hashlib
from dataclasses import asdict
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from core.canonical.datum import DatumType, DepthDatum
from core.canonical.position import Top
from core.canonical.reanchor import Change, Hazard, reproject
from core.canonical.survey import Station, tvd_at

VERSION = "reanchor@1.0.0"


def _datum(conn: psycopg.Connection, wellbore_id: int) -> DepthDatum:
    row = conn.execute(
        "SELECT coalesce(b.datum_type, 'RT'), coalesce(b.datum_elevation_m, w.rt_elevation_m)"
        " FROM wellbore b JOIN well w ON w.id = b.well_id WHERE b.id = %s",
        (wellbore_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"wellbore {wellbore_id} not found")
    return DepthDatum(DatumType(row[0]), float(row[1]))


def _survey(conn: psycopg.Connection, wellbore_id: int) -> list[Station]:
    """Best station per MD (newest non-rejected source), as the trajectory uses."""
    rows = conn.execute(
        "SELECT DISTINCT ON (s.md) s.md, s.inc, s.azi FROM survey_station s"
        " JOIN source_ref r ON r.id = s.source_ref_id"
        " WHERE s.wellbore_id = %s AND r.review_status <> 'rejected'"
        " ORDER BY s.md, s.source_ref_id DESC",
        (wellbore_id,),
    ).fetchall()
    return [Station(float(md), float(inc), float(azi)) for md, inc, azi in rows]


def _tops(conn: psycopg.Connection, wellbore_id: int) -> list[Top]:
    """Current top per formation: a human pick wins over extracted ones, newest first."""
    rows = conn.execute(
        "SELECT DISTINCT ON (t.formation_id) f.name, t.tvdss FROM formation_top t"
        " JOIN formation f ON f.id = t.formation_id JOIN source_ref r ON r.id = t.source_ref_id"
        " WHERE t.wellbore_id = %s AND t.tvdss IS NOT NULL AND r.review_status <> 'rejected'"
        " ORDER BY t.formation_id, (r.extractor_version = 'human') DESC, t.id DESC",
        (wellbore_id,),
    ).fetchall()
    return [Top(name, float(tvdss)) for name, tvdss in rows]


def reanchor_wellbore(conn: psycopg.Connection, wellbore_id: int) -> list[Change]:
    """Re-project every formation-relative hazard of a wellbore against its current tops."""
    rows = conn.execute(
        "SELECT h.id, f.name, h.rel_top, h.rel_base, h.tvdss_top, h.tvdss_base, h.md_top,"
        " h.md_base FROM hazard_profile h JOIN formation f ON f.id = h.formation_id"
        " WHERE h.wellbore_id = %s AND h.rel_top IS NOT NULL",
        (wellbore_id,),
    ).fetchall()
    hazards = [Hazard(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]) for r in rows]
    survey = _survey(conn, wellbore_id)
    changes = reproject(hazards, _tops(conn, wellbore_id), survey or None,
                        _datum(conn, wellbore_id))  # fmt: skip
    for c in changes:  # also stamps unchanged hazards, so they are not seen as stale again
        conn.execute(
            "UPDATE hazard_profile SET tvdss_top = coalesce(%s, tvdss_top),"
            " tvdss_base = coalesce(%s, tvdss_base),"
            " md_top = coalesce(%s, md_top), md_base = coalesce(%s, md_base), updated_at = now()"
            " WHERE id = %s",
            (c.after[0], c.after[1], c.md[0], c.md[1], c.hazard_id),
        )
    return changes


def _picks_document(conn: psycopg.Connection, wellbore_id: int) -> int:
    """The per-well 'top picks' document every human pick cites (created on first use)."""
    well_id, name, block, jv = conn.execute(
        "SELECT w.id, w.name, w.block_id, w.jv_id FROM wellbore b JOIN well w ON w.id = b.well_id"
        " WHERE b.id = %s",
        (wellbore_id,),
    ).fetchone()  # type: ignore[misc]
    digest = hashlib.sha256(f"top-picks:well:{well_id}".encode()).digest()
    row = conn.execute(
        "INSERT INTO document (sha256, type, title, uri, block_id, jv_id, classification,"
        " source) VALUES (%s, 'other', %s, %s, %s, %s, 'internal', 'human')"
        " ON CONFLICT (sha256) DO UPDATE SET title = excluded.title RETURNING id",
        (digest, f"Top picks, well {name}", f"internal://top-picks/{well_id}", block, jv),
    ).fetchone()
    assert row is not None
    return int(row[0])


def pick_top(conn: psycopg.Connection, wellbore_id: int, formation_id: int, md: float,
             picked_by: str) -> dict[str, Any]:  # fmt: skip
    """Record a geologist's top pick and re-project the wellbore's hazards, atomically."""
    with conn.transaction():
        survey = _survey(conn, wellbore_id)
        datum = _datum(conn, wellbore_id)
        tvd = tvd_at(survey, md) if survey and survey[0].md <= md <= survey[-1].md else md
        note = None if survey and survey[0].md <= md <= survey[-1].md else (
            "no survey at this MD: TVD taken equal to MD (vertical)")  # fmt: skip
        src = conn.execute(
            "INSERT INTO source_ref (document_id, extractor_version, confidence, review_status,"
            " reviewer, reviewed_at) VALUES (%s, 'human', 1.0, 'accepted', %s, now())"
            " RETURNING id",
            (_picks_document(conn, wellbore_id), picked_by),
        ).fetchone()
        assert src is not None
        tvdss = tvd - datum.elevation_m
        conn.execute(
            "INSERT INTO formation_top (wellbore_id, formation_id, md, tvd, tvdss, source_ref_id)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (wellbore_id, formation_id, md, tvd, tvdss, src[0]),
        )
        changes = reanchor_wellbore(conn, wellbore_id)
        moved = [asdict(c) for c in changes if c.after != c.before]
        conn.execute(
            "INSERT INTO audit_log (actor, action, entity, entity_id, payload)"
            " VALUES (%s, 'pick_top', 'wellbore', %s, %s)",
            (picked_by, str(wellbore_id),
             Jsonb({"formation_id": formation_id, "md": md, "tvdss": tvdss,
                    "hazards_moved": len(moved), "version": VERSION})),
        )  # fmt: skip
    return {"tvd": tvd, "tvdss": tvdss, "note": note, "changes": moved}


def stale_wellbores(conn: psycopg.Connection) -> list[int]:
    """Wellbores with a human top pick newer than their last hazard re-projection."""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT t.wellbore_id FROM formation_top t"
        " JOIN source_ref r ON r.id = t.source_ref_id AND r.extractor_version = 'human'"
        " WHERE EXISTS (SELECT 1 FROM hazard_profile h WHERE h.wellbore_id = t.wellbore_id"
        "   AND h.rel_top IS NOT NULL AND (h.updated_at IS NULL OR h.updated_at < r.reviewed_at))"
    )]  # fmt: skip
