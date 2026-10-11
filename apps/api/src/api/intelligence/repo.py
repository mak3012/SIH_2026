"""Database reads for the intelligence services. All queries run inside api.db.scoped(), so
row-level security limits every offset and every piece of evidence to the caller's blocks/JVs."""

from datetime import datetime, timedelta
from typing import Any

import psycopg

from api.intelligence.hazards import ActiveFormation, OffsetEvent, OffsetWell
from core.canonical.datum import DatumType, DepthDatum
from core.canonical.survey import Station, md_at_tvd, tvd_at


def survey(conn: psycopg.Connection, wellbore_id: int) -> list[Station]:
    rows = conn.execute(
        "SELECT DISTINCT ON (s.md) s.md, s.inc, s.azi FROM survey_station s"
        " JOIN source_ref r ON r.id = s.source_ref_id"
        " WHERE s.wellbore_id = %s AND r.review_status <> 'rejected'"
        " ORDER BY s.md, s.source_ref_id DESC",
        (wellbore_id,),
    ).fetchall()
    return [Station(float(a), float(b), float(c)) for a, b, c in rows]


def datum(conn: psycopg.Connection, wellbore_id: int) -> DepthDatum:
    row = conn.execute(
        "SELECT coalesce(b.datum_type, 'RT'), coalesce(b.datum_elevation_m, w.rt_elevation_m)"
        " FROM wellbore b JOIN well w ON w.id = b.well_id WHERE b.id = %s",
        (wellbore_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"wellbore {wellbore_id} not found or not visible")
    return DepthDatum(DatumType(row[0]), float(row[1]))


def well_name(conn: psycopg.Connection, wellbore_id: int) -> str:
    row = conn.execute("SELECT b.name FROM wellbore b WHERE b.id = %s", (wellbore_id,)).fetchone()
    if row is None:
        raise LookupError(f"wellbore {wellbore_id} not found or not visible")
    return str(row[0])


def active_formations(
    conn: psycopg.Connection, wellbore_id: int, td_md: float | None = None
) -> list[ActiveFormation]:
    """Formations along the active wellbore's plan: its current tops (a human pick wins over
    the prognosis), each based at the next top or TD, with MD from the planned survey."""
    rows = conn.execute(
        "SELECT DISTINCT ON (t.formation_id) f.name, t.md, t.tvdss FROM formation_top t"
        " JOIN formation f ON f.id = t.formation_id JOIN source_ref r ON r.id = t.source_ref_id"
        " WHERE t.wellbore_id = %s AND r.review_status <> 'rejected'"
        " ORDER BY t.formation_id, (r.extractor_version = 'human') DESC, t.id DESC",
        (wellbore_id,),
    ).fetchall()
    stations, d = survey(conn, wellbore_id), datum(conn, wellbore_id)
    if td_md is None:
        r = conn.execute("SELECT td_md FROM wellbore WHERE id = %s", (wellbore_id,)).fetchone()
        td_md = r[0] if r and r[0] else (stations[-1].md if stations else None)
    tops = []
    for name, md, tvdss in rows:
        if tvdss is not None and stations:
            try:
                md = md_at_tvd(stations, tvdss + d.elevation_m)
            except ValueError:
                pass
        tops.append((float(md), name, tvdss))
    tops.sort()
    out = []
    for i, (md, name, tvdss) in enumerate(tops):
        base_md = tops[i + 1][0] if i + 1 < len(tops) else td_md
        base_tvdss = (
            tops[i + 1][2]
            if i + 1 < len(tops)
            else (
                tvd_at(stations, td_md) - d.elevation_m
                if stations and td_md is not None and stations[0].md <= td_md <= stations[-1].md
                else None
            )
        )
        out.append(ActiveFormation(name, md, base_md, tvdss, base_tvdss))
    return out


def offsets(conn: psycopg.Connection, weights: list[tuple[int, float]]) -> list[OffsetWell]:
    """Offset data for the selected wells: tops, TD, events with mitigations and sources,
    documented intervals. Main wellbores only."""
    out = []
    for well_id, weight in weights:
        row = conn.execute(
            "SELECT b.id, w.name, b.td_md FROM well w JOIN wellbore b ON b.well_id = w.id"
            " AND b.parent_id IS NULL WHERE w.id = %s",
            (well_id,),
        ).fetchone()
        if row is None:
            continue  # not visible to this caller
        bore, name, td = row
        tops = {
            n: float(md)
            for n, md in conn.execute(
                "SELECT DISTINCT ON (f.name) f.name, t.md FROM formation_top t"
                " JOIN formation f ON f.id = t.formation_id WHERE t.wellbore_id = %s"
                " ORDER BY f.name, t.id DESC",
                (bore,),
            )
        }
        events = []
        for eid, typ, md, form, rel, sev, doc, page, sref in conn.execute(
            "SELECT e.id, e.type::text, e.md_top, f.name, e.rel_pos, e.severity, d.title,"
            " r.page, e.source_ref_id FROM event e LEFT JOIN formation f ON f.id = e.formation_id"
            " JOIN source_ref r ON r.id = e.source_ref_id"
            " LEFT JOIN document d ON d.id = r.document_id"
            " WHERE e.wellbore_id = %s AND e.md_top IS NOT NULL AND r.review_status <> 'rejected'",
            (bore,),
        ):
            mits = [
                (a, eff)
                for a, eff in conn.execute(
                    "SELECT action, effective FROM mitigation WHERE event_id = %s", (eid,)
                )
            ]
            if rel is None and form in tops:
                deeper = sorted(v for v in tops.values() if v > tops[form])
                base = deeper[0] if deeper else td
                if base and base > tops[form]:
                    rel = (md - tops[form]) / (base - tops[form])
            events.append(
                OffsetEvent(
                    eid,
                    typ,
                    float(md),
                    form,
                    rel,
                    sev,
                    mits,
                    {"document": doc, "page": page, "source_ref_id": sref},
                )
            )
        documented = [
            (float(a), float(b))
            for a, b in conn.execute(
                "SELECT md_top, md_base FROM event_documentation WHERE wellbore_id = %s", (bore,)
            )
        ]
        out.append(
            OffsetWell(
                well_id,
                name,
                weight,
                tops,
                sorted(tops, key=lambda f: tops[f]),
                float(td) if td else None,
                events,
                documented,
            )
        )
    return out


def basin_rates(
    conn: psycopg.Connection, exclude_wellbores: list[int] | None = None
) -> dict[tuple[str, str], tuple[int, int]]:
    """(formation, hazard) -> (wells with an event there, wells with it documented or an
    event there), over every visible well except `exclude_wellbores` (leave-one-well-out
    evaluation must not see the held-out well): the prior's centre."""
    rows = conn.execute(
        "WITH iv AS ("
        "  SELECT t.wellbore_id, f.name AS formation, t.md AS top,"
        "    coalesce(lead(t.md) OVER (PARTITION BY t.wellbore_id ORDER BY t.md), b.td_md) AS base"
        "  FROM formation_top t JOIN formation f ON f.id = t.formation_id"
        "  JOIN wellbore b ON b.id = t.wellbore_id"
        "  WHERE NOT (t.wellbore_id = ANY(%(excl)s))),"
        " ev AS (SELECT DISTINCT e.wellbore_id, coalesce(f.name, iv.formation) AS formation,"
        "          e.type::text AS hazard FROM event e"
        "        LEFT JOIN formation f ON f.id = e.formation_id"
        "        LEFT JOIN iv ON iv.wellbore_id = e.wellbore_id AND e.md_top >= iv.top"
        "          AND e.md_top < iv.base),"
        " doc AS (SELECT DISTINCT iv.wellbore_id, iv.formation FROM iv"
        "          JOIN event_documentation d"
        "          ON d.wellbore_id = iv.wellbore_id AND d.md_top <= iv.top"
        "          AND d.md_base >= iv.base)"
        " SELECT f.formation, h.hazard,"
        "   count(*) FILTER (WHERE ev.wellbore_id IS NOT NULL),"
        "   count(*) FILTER (WHERE ev.wellbore_id IS NOT NULL OR doc.wellbore_id IS NOT NULL)"
        " FROM (SELECT DISTINCT wellbore_id, formation FROM iv) f"
        " CROSS JOIN (SELECT unnest(enum_range(NULL::event_type))::text AS hazard) h"
        " LEFT JOIN ev ON ev.wellbore_id = f.wellbore_id AND ev.formation = f.formation"
        "   AND ev.hazard = h.hazard"
        " LEFT JOIN doc ON doc.wellbore_id = f.wellbore_id AND doc.formation = f.formation"
        " GROUP BY f.formation, h.hazard",
        {"excl": exclude_wellbores or []},
    ).fetchall()
    return {(f, h): (int(k), int(n)) for f, h, k, n in rows if n}


def evidence(conn: psycopg.Connection, well_ids: list[int]) -> list[dict[str, Any]]:
    """Offset LOT/FIT results and loss / kick / stuck-pipe events, with their sources."""
    items: list[dict[str, Any]] = []
    for kind, md, value, form, well, doc, page, sref in conn.execute(
        "SELECT l.test_type, l.md, l.emw, f.name, w.name, d.title, r.page, l.source_ref_id"
        " FROM lot_fit l JOIN wellbore b ON b.id = l.wellbore_id JOIN well w ON w.id = b.well_id"
        " JOIN source_ref r ON r.id = l.source_ref_id LEFT JOIN document d ON d.id = r.document_id"
        " LEFT JOIN LATERAL (SELECT fm.name FROM formation_top t JOIN formation fm"
        "   ON fm.id = t.formation_id WHERE t.wellbore_id = l.wellbore_id AND t.md <= l.md"
        "   ORDER BY t.md DESC LIMIT 1) f ON true"
        " WHERE w.id = ANY(%s) AND r.review_status <> 'rejected' ORDER BY w.name, l.md",
        (well_ids,),
    ):
        items.append(
            {
                "kind": kind,
                "well": well,
                "formation": form,
                "md": round(md, 1),
                "value": value,
                "unit": "sg",
                "source": {"document": doc, "page": page, "source_ref_id": sref},
            }
        )
    for kind, md, mw, ecd, form, well, doc, page, sref in conn.execute(
        "SELECT e.type::text, e.md_top, e.mw, e.ecd, f.name, w.name, d.title, r.page,"
        " e.source_ref_id FROM event e JOIN wellbore b ON b.id = e.wellbore_id"
        " JOIN well w ON w.id = b.well_id LEFT JOIN formation f ON f.id = e.formation_id"
        " JOIN source_ref r ON r.id = e.source_ref_id LEFT JOIN document d ON d.id = r.document_id"
        " WHERE w.id = ANY(%s) AND e.type IN ('loss', 'kick', 'stuck_pipe')"
        " AND r.review_status <> 'rejected' ORDER BY w.name, e.md_top",
        (well_ids,),
    ):
        # Losses: the ECD at which returns were lost (MW if ECD was not recorded).
        # Kicks: the mud weight that did not hold the formation.
        value = (ecd or mw) if kind == "loss" else mw
        items.append(
            {
                "kind": kind,
                "well": well,
                "formation": form,
                "md": None if md is None else round(md, 1),
                "value": value,
                "unit": "sg" if value is not None else None,
                "source": {"document": doc, "page": page, "source_ref_id": sref},
            }
        )
    return items


def progress(
    conn: psycopg.Connection, wellbore_id: int, window_h: float = 12.0
) -> tuple[float | None, float | None, str, datetime | None]:
    """(current hole MD, rolling rate m/h, basis, as-of time). Real-time data first: net
    progress over the last `window_h` hours; else daily reports: last three days."""
    last = conn.execute(
        "SELECT ts, coalesce(hole_md, bit_md) FROM rt_measurement WHERE wellbore_id = %s"
        " AND coalesce(hole_md, bit_md) IS NOT NULL ORDER BY ts DESC LIMIT 1",
        (wellbore_id,),
    ).fetchone()
    if last is not None:
        ts, md = last
        first = conn.execute(
            "SELECT ts, coalesce(hole_md, bit_md) FROM rt_measurement WHERE wellbore_id = %s"
            " AND ts >= %s AND coalesce(hole_md, bit_md) IS NOT NULL ORDER BY ts LIMIT 1",
            (wellbore_id, ts - timedelta(hours=window_h)),
        ).fetchone()
        hours = (ts - first[0]).total_seconds() / 3600 if first else 0
        rate = (md - first[1]) / hours if first and hours > 0 else None
        return float(md), rate, f"real-time, net progress over the last {window_h:g} h", ts
    rows = conn.execute(
        "SELECT ts, md FROM mud_record WHERE wellbore_id = %s AND md IS NOT NULL"
        " ORDER BY ts DESC LIMIT 4",
        (wellbore_id,),
    ).fetchall()
    if rows:
        (t1, m1), (t0, m0) = rows[0], rows[-1]
        hours = (t1 - t0).total_seconds() / 3600
        rate = (m1 - m0) / hours if hours > 0 else None
        return float(m1), rate, "daily reports, last three days", t1
    return None, None, "no progress data", None
