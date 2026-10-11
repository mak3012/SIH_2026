"""Endpoints the web workbench needs beyond the domain APIs:

    GET  /wellbores/{id}/scene              3D geometry: trajectories, tops, events, hazard bands
    GET  /review/items                      extraction review queue (open first)
    POST /review/items/{id}                 accept / correct / reject, audited
    GET  /documents/{id}/pages/{page}.png   a source page, for evidence chips
    GET  /admin/status                      feeds, models, migration, counts (admin)

All under the caller's row-level security (api.intelligence.routes.db).
"""

import io
import math
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import psycopg
from fastapi import APIRouter, HTTPException, Query, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from api import audit, storage
from api.auth import CurrentUser, User
from api.intelligence import analogues as an
from api.intelligence import hazards as hz
from api.intelligence import repo
from api.intelligence.routes import Conn

router = APIRouter(tags=["workbench"])
SCENE_RADIUS_M = 10_000.0
SCENE_MAX_WELLS = 12
EARTH_M = 111_320.0


# ------------------------------------------------------------------ 3D scene


def _path(
    conn: psycopg.Connection, bore: int, lat0: float, lon0: float
) -> list[tuple[float, float, float, float]]:
    """(md, x east m, y north m, z m above MSL) along the wellbore; vertical below the surface
    location when there is no survey."""
    rows = conn.execute(
        "SELECT DISTINCT ON (md) md, ST_X(geom), ST_Y(geom), tvdss FROM survey_station"
        " WHERE wellbore_id = %s ORDER BY md, id",
        (bore,),
    ).fetchall()
    kx = EARTH_M * math.cos(math.radians(lat0))
    if rows:
        return [
            (float(md), (lon - lon0) * kx, (lat - lat0) * EARTH_M, -float(ss))
            for md, lon, lat, ss in rows
        ]
    w = conn.execute(
        "SELECT ST_X(w.surface), ST_Y(w.surface), w.rt_elevation_m, b.td_md FROM wellbore b"
        " JOIN well w ON w.id = b.well_id WHERE b.id = %s",
        (bore,),
    ).fetchone()
    if w is None or w[3] is None:
        return []
    x, y, rt = (w[0] - lon0) * kx, (w[1] - lat0) * EARTH_M, float(w[2] or 0)
    return [(0.0, x, y, rt), (float(w[3]), x, y, rt - float(w[3]))]


def _at(path: list[tuple[float, float, float, float]], md: float) -> list[float] | None:
    if not path:
        return None
    if md <= path[0][0]:
        return list(path[0][1:])
    for a, b in zip(path, path[1:], strict=False):
        if a[0] <= md <= b[0]:
            f = (md - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
            return [a[i] + f * (b[i] - a[i]) for i in (1, 2, 3)]
    return list(path[-1][1:])


def _segment(
    path: list[tuple[float, float, float, float]], md0: float, md1: float
) -> list[list[float]]:
    pts = [_at(path, md0)] + [list(p[1:]) for p in path if md0 < p[0] < md1] + [_at(path, md1)]
    return [p for p in pts if p is not None]


@router.get("/wellbores/{wellbore_id}/scene")
def scene(wellbore_id: int, conn: psycopg.Connection = Conn) -> dict[str, Any]:
    """Subsurface scene in local metres (x east, y north, z above MSL) around the wellbore:
    its trajectory and its offsets' (latest selection, else wells within 10 km), formation
    tops, offset events, the roadmap's hazard bands on this wellbore, and the bit."""
    head = conn.execute(
        "SELECT w.id, w.name, ST_Y(w.surface), ST_X(w.surface) FROM wellbore b"
        " JOIN well w ON w.id = b.well_id WHERE b.id = %s",
        (wellbore_id,),
    ).fetchone()
    if head is None:
        raise HTTPException(404, "wellbore not found")
    well_id, name, lat0, lon0 = head
    sel = conn.execute(
        "SELECT id FROM offset_selection WHERE active_wellbore_id = %s ORDER BY id DESC LIMIT 1",
        (wellbore_id,),
    ).fetchone()
    if sel:
        others = [w for w, _ in an.selected_offsets(conn, int(sel[0]))]
    else:
        others = [
            int(r[0])
            for r in conn.execute(
                "SELECT w.id FROM well w, well a WHERE a.id = %s AND w.id <> a.id"
                " AND ST_DWithin(w.surface::geography, a.surface::geography, %s)"
                " ORDER BY ST_Distance(w.surface::geography, a.surface::geography) LIMIT %s",
                (well_id, SCENE_RADIUS_M, SCENE_MAX_WELLS),
            )
        ]
    wells = []
    for wid in [well_id, *others[:SCENE_MAX_WELLS]]:
        bore = conn.execute(
            "SELECT b.id, w.name FROM wellbore b JOIN well w ON w.id = b.well_id"
            " WHERE w.id = %s ORDER BY b.parent_id NULLS FIRST, b.id LIMIT 1",
            (wid,),
        ).fetchone()
        if bore is None:
            continue
        bid = wellbore_id if wid == well_id else int(bore[0])
        path = _path(conn, bid, float(lat0), float(lon0))
        tops = [
            {"formation": f, "md": float(md), "at": _at(path, float(md))}
            for f, md in conn.execute(
                "SELECT DISTINCT ON (f.name) f.name, t.md FROM formation_top t"
                " JOIN formation f ON f.id = t.formation_id WHERE t.wellbore_id = %s"
                " ORDER BY f.name, t.id DESC",
                (bid,),
            )
        ]
        events = [
            {
                "id": eid,
                "type": t,
                "md": float(md),
                "severity": sev,
                "formation": f,
                "document": doc,
                "document_id": did,
                "page": page,
                "at": _at(path, float(md)),
            }
            for eid, t, md, sev, f, doc, did, page in conn.execute(
                "SELECT e.id, e.type::text, e.md_top, e.severity, f.name, d.title, d.id, r.page"
                " FROM event e LEFT JOIN formation f ON f.id = e.formation_id"
                " JOIN source_ref r ON r.id = e.source_ref_id"
                " LEFT JOIN document d ON d.id = r.document_id"
                " WHERE e.wellbore_id = %s AND e.md_top IS NOT NULL ORDER BY e.md_top",
                (bid,),
            )
        ]
        wells.append(
            {
                "wellbore_id": bid,
                "well": bore[1],
                "active": wid == well_id,
                "path": [list(p[1:]) for p in path],
                "tops": tops,
                "events": events,
            }
        )
    bands = []
    if sel:
        active_path = _path(conn, wellbore_id, float(lat0), float(lon0))
        forms = repo.active_formations(conn, wellbore_id, None)
        offs = repo.offsets(conn, an.selected_offsets(conn, int(sel[0])))
        for c in hz.roadmap(forms, offs, basin_rates=repo.basin_rates(conn)):
            if c.band_md and c.mean >= 0.05:
                bands.append(
                    {
                        "hazard": c.hazard,
                        "formation": c.formation,
                        "mean": round(c.mean, 3),
                        "md": list(c.band_md),
                        "path": _segment(active_path, *c.band_md),
                    }
                )
    bit = conn.execute(
        "SELECT coalesce(hole_md, bit_md), ts FROM rt_measurement WHERE wellbore_id = %s"
        " ORDER BY ts DESC LIMIT 1",
        (wellbore_id,),
    ).fetchone()
    active = next(w for w in wells if w["active"])
    order = sorted(
        {t["formation"] for w in wells for t in w["tops"]},
        key=lambda f: min(t["md"] for w in wells for t in w["tops"] if t["formation"] == f),
    )
    return {
        "wellbore_id": wellbore_id,
        "well": name,
        "origin": {"lat": lat0, "lon": lon0},
        "formations": order,
        "wells": wells,
        "bands": bands,
        "bit": None
        if bit is None or bit[0] is None
        else {
            "md": float(bit[0]),
            "ts": bit[1],
            "at": _at(_path(conn, wellbore_id, float(lat0), float(lon0)), float(bit[0])),
        },
        "selection_id": int(sel[0]) if sel else None,
        "active_td": active["path"][-1] if active["path"] else None,
    }


# ------------------------------------------------------------------ review queue


# What a wrong value would cost downstream: depths and pressures feed the roadmap, the linter
# and the live alarms; event facts feed hazard rates; names and dates matter least.
VALUE_WEIGHTS = (
    (("md", "depth", "top", "shoe", "tvd"), 3.0),
    (("mw", "ecd", "emw", "lot", "fit", "pressure", "density"), 3.0),
    (("loss", "kick", "stuck", "event", "npt", "severity"), 2.5),
    (("casing", "hole", "bit", "od"), 2.0),
)


def review_value(field: str, confidence: float, failures: int) -> float:
    """Priority of a review item: field importance x doubt x (1 + 0.5 per failed rule)."""
    f = field.lower()
    weight = next((w for keys, w in VALUE_WEIGHTS if any(k in f for k in keys)), 1.0)
    return round(weight * (1 - confidence) * (1 + 0.5 * failures), 4)


@router.get("/review/items")
def review_items(
    status: str = Query("open", pattern="^(open|accepted|corrected|rejected|all)$"),
    order: str = Query("value", pattern="^(value|confidence)$"),
    limit: int = Query(100, ge=1, le=500),
    conn: psycopg.Connection = Conn,
) -> list[dict[str, Any]]:
    """Extraction output held for a human (below the confidence threshold, failing a rule, or
    not attachable). Open items first, then by `value` (review_value) or lowest confidence.
    `conflict` is true when the extraction passes disagreed or a rule failed."""
    cur = conn.execute(
        "SELECT r.id, r.kind, r.doc_type, r.field, r.value, r.printed, r.page, r.bbox, r.t_start,"
        " r.t_end, r.confidence, r.components, r.rule_failures, r.reason, r.status, r.resolved_by,"
        " r.resolved_at, r.document_id, d.title, r.audio_note_id, r.created_at"
        " FROM review_item r LEFT JOIN document d ON d.id = r.document_id"
        " WHERE (%(s)s = 'all' OR r.status = %(s)s)",
        {"s": status},
    )
    cols = [d.name for d in cur.description or []]
    items = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    for i in items:
        fails = len(i["rule_failures"] or [])
        i["value_score"] = review_value(i["field"], float(i["confidence"]), fails)
        i["conflict"] = fails > 0 or (i["components"] or {}).get("consistency") == 0.0
    key = "value_score" if order == "value" else "confidence"
    sign = -1 if order == "value" else 1
    items.sort(key=lambda i: (i["status"] != "open", sign * i[key], i["id"]))
    return items[:limit]


class Decision(BaseModel):
    action: str = Field(pattern="^(accept|correct|reject)$")
    value: Any = None
    note: str | None = Field(None, max_length=1000)


@router.post("/review/items/{item_id}")
def decide(
    item_id: int, d: Decision, user: User = CurrentUser, conn: psycopg.Connection = Conn
) -> dict[str, Any]:
    """Resolve a review item. `correct` needs the corrected value. The decision is audited;
    the pipeline applies resolved items to facts on its next run."""
    user.require("review")
    if d.action == "correct" and d.value is None:
        raise HTTPException(422, "a correction needs the corrected value")
    row = conn.execute("SELECT status, value FROM review_item WHERE id = %s", (item_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "review item not found")
    if row[0] != "open":
        raise HTTPException(409, f"already {row[0]}")
    status = {"accept": "accepted", "correct": "corrected", "reject": "rejected"}[d.action]
    conn.execute(
        "UPDATE review_item SET status = %s, value = coalesce(%s, value), resolved_by = %s,"
        " resolved_at = now() WHERE id = %s",
        (status, Jsonb(d.value) if d.action == "correct" else None, user.name, item_id),
    )
    audit.record(
        conn,
        user.name,
        f"review.{d.action}",
        "review_item",
        item_id,
        {"before": row[1], "after": d.value, "note": d.note},
    )
    return {"id": item_id, "status": status, "resolved_by": user.name}


# ------------------------------------------------------------------ source pages


@lru_cache(maxsize=64)
def _render(uri: str, page: int) -> bytes:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(storage.read(uri))
    if not 1 <= page <= len(pdf):
        raise IndexError(page)
    img = pdf[page - 1].render(scale=1.6).to_pil()
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


@router.get("/documents/{document_id}/pages/{page}.png")
def page_image(
    document_id: int, page: int, user: User = CurrentUser, conn: psycopg.Connection = Conn
) -> Response:
    """A document page as PNG (the cited box is drawn by the client from the citation bbox).
    404 when the document is not visible, or has no stored file (synthetic records)."""
    row = conn.execute("SELECT uri FROM document WHERE id = %s", (document_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "document not found")
    try:
        png = _render(str(row[0]), page)
    except storage.Missing as e:
        raise HTTPException(404, "no stored file for this document") from e
    except IndexError as e:
        raise HTTPException(404, "no such page") from e
    except Exception as e:  # storage unreachable, not a PDF
        raise HTTPException(502, f"could not render the page: {type(e).__name__}") from e
    audit.record(conn, user.name, "read.page", "document", document_id, {"page": page})
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


# ------------------------------------------------------------------ admin


@router.get("/admin/status")
def admin_status(user: User = CurrentUser, conn: psycopg.Connection = Conn) -> dict[str, Any]:
    user.require("admin")
    from api.realtime.detectors import DEFAULT_PATH

    bank = Path(os.environ.get("RT_DETECTORS", str(DEFAULT_PATH)))
    sha = bank.with_suffix(".sha256")
    feeds = conn.execute(
        "SELECT f.wellbore_id, w.name, f.connector, f.state, f.last_sample_ts, f.updated_at"
        " FROM rt_feed f JOIN wellbore b ON b.id = f.wellbore_id JOIN well w ON w.id = b.well_id"
        " ORDER BY f.updated_at DESC"
    ).fetchall()

    def one(sql: str) -> Any:
        row = conn.execute(sql).fetchone()
        return row[0] if row else None

    return {
        "user": {
            "name": user.name,
            "roles": sorted(user.roles),
            "blocks": user.blocks,
            "jvs": user.jvs,
        },
        "counts": {
            "wells": one("SELECT count(*) FROM well"),
            "documents": one("SELECT count(*) FROM document"),
            "open_review_items": one("SELECT count(*) FROM review_item WHERE status = 'open'"),
            "open_alerts": one(
                "SELECT count(*) FROM alert WHERE status = 'open' AND suppressed IS NULL"
            ),
        },
        "audit": audit.status(conn),
        "feeds": [
            dict(
                zip(
                    ["wellbore_id", "well", "connector", "state", "last_sample_ts", "updated_at"],
                    f,
                    strict=True,
                )
            )
            for f in feeds
        ],
        "detectors": {
            "path": str(bank),
            "present": bank.exists(),
            "sha256": sha.read_text().strip()[:12] if sha.exists() else None,
        },
        "llm": {
            "backend": os.environ.get("LLM_BACKEND", "none"),
            "model": os.environ.get("LLM_MODEL"),
            "embeddings": os.environ.get("EMBED_BACKEND", "hash"),
        },
    }
