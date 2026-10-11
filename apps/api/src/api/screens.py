"""Read models for the web screens (all under the caller's row-level security).

GET  /offsets/preview/{id}            offset map: wells, trajectories, radii, halos, ranked
                                      analogues with per-factor scores (nothing saved)
GET  /correlation/{id}                depth tracks of the active well and its offsets
GET  /active/{id}/history             recent real-time samples for the live charts
GET  /active/{id}/envelope            torque-and-drag hookload envelope; MW/ECD window
GET  /documents/{id}/file             the source PDF (for the evidence viewer)
GET  /documents/{id}/fields           what was extracted from it, with page and box
GET  /audio-notes/{id}                voice-note metadata and transcript spans
GET  /audio-notes/{id}/file           the recording
GET  /fleet                           per well: next hazard, ETA, alerts, freshness; digest
POST /voice-notes                     upload a recording captured in the app
GET  /voice-notes                     the caller's captured notes and their status
"""

import hashlib
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import psycopg
from fastapi import APIRouter, HTTPException, Query, Request, Response

from api import audit, storage
from api.auth import CurrentUser, User
from api.intelligence import analogues as an
from api.intelligence import hazards as hz
from api.intelligence import repo
from api.intelligence.routes import Conn, _formation_id
from api.realtime import context as rtctx
from api.realtime.channels import CHANNELS
from api.realtime.derived import torque_drag

router = APIRouter(tags=["screens"])
EARTH_M = 111_320.0
MAX_AUDIO = 20 * 1024 * 1024
AUDIO_TYPES = {"audio/webm", "audio/ogg", "audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp4"}


def _one(conn: psycopg.Connection, sql: str, params: Any = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else row[0]


def _main_bore(conn: psycopg.Connection, well_id: int) -> int | None:
    bore: int | None = _one(
        conn,
        "SELECT id FROM wellbore WHERE well_id = %s ORDER BY parent_id NULLS FIRST, id LIMIT 1",
        (well_id,),
    )
    return bore


def _geo_path(conn: psycopg.Connection, bore: int) -> list[tuple[float, float, float, float]]:
    """(md, lon, lat, z m above MSL) along the wellbore; vertical without a survey."""
    rows = conn.execute(
        "SELECT DISTINCT ON (md) md, ST_X(geom), ST_Y(geom), tvdss FROM survey_station"
        " WHERE wellbore_id = %s ORDER BY md, id",
        (bore,),
    ).fetchall()
    if rows:
        return [(float(md), float(x), float(y), -float(ss)) for md, x, y, ss in rows]
    w = conn.execute(
        "SELECT ST_X(w.surface), ST_Y(w.surface), coalesce(b.datum_elevation_m, w.rt_elevation_m),"
        " b.td_md FROM wellbore b JOIN well w ON w.id = b.well_id WHERE b.id = %s",
        (bore,),
    ).fetchone()
    if w is None or w[3] is None:
        return []
    x, y, rt = float(w[0]), float(w[1]), float(w[2] or 0)
    return [(0.0, x, y, rt), (float(w[3]), x, y, rt - float(w[3]))]


def _interp(path: list[tuple[float, float, float, float]], md: float) -> list[float] | None:
    if not path:
        return None
    mds = [p[0] for p in path]
    return [float(np.interp(md, mds, [p[i] for p in path])) for i in (1, 2, 3)]


# ------------------------------------------------------------------ offset map


@router.get("/offsets/preview/{wellbore_id}")
def offsets_preview(
    wellbore_id: int,
    formation: str,
    surface_radius_m: float = Query(10_000, gt=0, le=100_000),
    subsurface_radius_m: float = Query(5_000, gt=0, le=100_000),
    conn: psycopg.Connection = Conn,
) -> dict[str, Any]:
    """Everything the offset map draws: the visible wells (surface, datum uncertainty, fault
    block, evidence count, trajectory), where each crosses the target top, and the analogue
    ranking with per-factor scores, at the given radii. Read-only: selecting stays a POST."""
    fid, fname = _formation_id(conn, formation)
    try:
        active = an.active_context(conn, wellbore_id, fid)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    cands = an.candidates(conn, active, surface_m=surface_radius_m)
    ranked = an.rank(
        active,
        cands,
        {},
        subsurface_m=subsurface_radius_m,
        pinned=set(),
        excluded=set(),
    )
    by_well = {r.features.well_id: r for r in ranked}
    wells = []
    for wid, name, block, fb, conf, unc, lon, lat, n_ev, n_src in conn.execute(
        "SELECT w.id, w.name, w.block_id, w.fault_block, w.datum_confidence,"
        " w.position_uncertainty_m, ST_X(w.surface), ST_Y(w.surface),"
        " (SELECT count(*) FROM event e JOIN wellbore b ON b.id = e.wellbore_id"
        "   WHERE b.well_id = w.id),"
        " (SELECT count(*) FROM formation_top t JOIN wellbore b ON b.id = t.wellbore_id"
        "   WHERE b.well_id = w.id)"
        " + (SELECT count(*) FROM event e JOIN wellbore b ON b.id = e.wellbore_id"
        "   WHERE b.well_id = w.id)"
        " + (SELECT count(*) FROM casing_string c JOIN wellbore b ON b.id = c.wellbore_id"
        "   WHERE b.well_id = w.id)"
        " + (SELECT count(*) FROM lot_fit l JOIN wellbore b ON b.id = l.wellbore_id"
        "   WHERE b.well_id = w.id)"
        " + (SELECT count(*) FROM mud_record m JOIN wellbore b ON b.id = m.wellbore_id"
        "   WHERE b.well_id = w.id)"
        " FROM well w ORDER BY w.name"
    ):
        bore = wellbore_id if wid == active.well_id else _main_bore(conn, wid)
        path = _geo_path(conn, bore) if bore else []
        top = (
            _one(
                conn,
                "SELECT md FROM formation_top WHERE wellbore_id = %s AND formation_id = %s"
                " ORDER BY id DESC LIMIT 1",
                (bore, fid),
            )
            if bore
            else None
        )
        if top is None and wid == active.well_id:
            forms = repo.active_formations(conn, wellbore_id, None)
            top = next((f.top_md for f in forms if f.formation == fname), None)
        r = by_well.get(wid)
        wells.append(
            {
                "well_id": wid,
                "well": name,
                "wellbore_id": bore,
                "active": wid == active.well_id,
                "block": block,
                "fault_block": fb,
                "lon": lon,
                "lat": lat,
                "datum_confidence": conf,
                "position_uncertainty_m": unc,
                "events": n_ev,
                "evidence": n_src,
                "path": [[round(p[1], 6), round(p[2], 6), round(p[3], 1)] for p in path],
                "at_formation": None if top is None else _interp(path, float(top)),
                "candidate": None
                if r is None
                else {
                    "rank": r.rank,
                    "score": round(r.score, 4),
                    "included": r.included,
                    "within_radii": r.within_radii,
                    "surface_m": round(r.features.surface_m),
                    "formation_m": None
                    if r.features.formation_m is None
                    else round(r.features.formation_m),
                    "factors": [
                        {
                            "name": f.name,
                            "weight": f.weight,
                            "score": round(f.score, 3),
                            "unknown": f.unknown,
                            "contribution": round(f.contribution, 4),
                            "why": f.why,
                        }
                        for f in r.factors
                    ],
                },
            }
        )
    return {
        "wellbore_id": wellbore_id,
        "formation": fname,
        "surface_radius_m": surface_radius_m,
        "subsurface_radius_m": subsurface_radius_m,
        "weights": an.DEFAULT_WEIGHTS,
        "wells": wells,
    }


# ------------------------------------------------------------------ correlation


def _tvdss_of(conn: psycopg.Connection, bore: int) -> Any:
    """md -> TVDSS (m below MSL) from the survey, else vertical below the depth datum."""
    rows = conn.execute(
        "SELECT DISTINCT ON (md) md, tvdss FROM survey_station WHERE wellbore_id = %s"
        " ORDER BY md, id",
        (bore,),
    ).fetchall()
    if len(rows) >= 2:
        mds, ss = [float(r[0]) for r in rows], [float(r[1]) for r in rows]
        return lambda md: None if md is None else round(float(np.interp(md, mds, ss)), 1)
    elev = float(
        _one(
            conn,
            "SELECT coalesce(b.datum_elevation_m, w.rt_elevation_m) FROM wellbore b"
            " JOIN well w ON w.id = b.well_id WHERE b.id = %s",
            (bore,),
        )
        or 0
    )
    return lambda md: None if md is None else round(md - elev, 1)


def _track(conn: psycopg.Connection, bore: int, name: str, td: float | None) -> dict[str, Any]:
    tv = _tvdss_of(conn, bore)
    tops = [
        {"formation": f, "md": round(md, 1), "tvdss": tv(md)}
        for f, md in conn.execute(
            "SELECT DISTINCT ON (f.name) f.name, t.md FROM formation_top t"
            " JOIN formation f ON f.id = t.formation_id WHERE t.wellbore_id = %s"
            " ORDER BY f.name, t.id DESC",
            (bore,),
        )
    ]
    tops.sort(key=lambda t: t["md"])
    cur = conn.execute(
        "SELECT e.id, e.type::text AS type, round(e.md_top::numeric, 1)::float AS md,"
        " e.severity, f.name AS formation, e.narrative, e.mw, e.ecd, e.loss_bbl, e.npt_hr,"
        " d.title AS document, d.id AS document_id, r.page, r.bbox,"
        " r.review_status::text AS review_status"
        " FROM event e LEFT JOIN formation f ON f.id = e.formation_id"
        " JOIN source_ref r ON r.id = e.source_ref_id"
        " LEFT JOIN document d ON d.id = r.document_id"
        " WHERE e.wellbore_id = %s AND e.md_top IS NOT NULL AND r.review_status <> 'rejected'"
        " ORDER BY e.md_top",
        (bore,),
    )
    names = [c.name for c in cur.description or []]
    events = [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
    for e in events:
        e["tvdss"] = tv(e["md"])
    casing = [
        {"kind": k, "od_in": od, "md": round(md, 1), "tvdss": tv(md)}
        for k, od, md in conn.execute(
            "SELECT kind, od_in, md_shoe FROM casing_string WHERE wellbore_id = %s"
            " ORDER BY md_shoe",
            (bore,),
        )
    ]
    mud = [
        {"md": round(md, 1), "tvdss": tv(md), "mw": round(mw, 3)}
        for md, mw in conn.execute(
            "SELECT md, mw FROM mud_record WHERE wellbore_id = %s AND md IS NOT NULL ORDER BY md",
            (bore,),
        )
    ]
    lot = [
        {"type": t, "md": round(md, 1), "tvdss": tv(md), "emw": round(emw, 3)}
        for t, md, emw in conn.execute(
            "SELECT test_type, md, emw FROM lot_fit WHERE wellbore_id = %s ORDER BY md", (bore,)
        )
    ]
    return {
        "wellbore_id": bore,
        "well": name,
        "td_md": td,
        "td_tvdss": tv(td) if td else None,
        "tops": tops,
        "events": events,
        "casing": casing,
        "mud": mud,
        "ecd": [{"md": e["md"], "tvdss": e["tvdss"], "ecd": e["ecd"]} for e in events if e["ecd"]],
        "lot": lot,
    }


@router.get("/correlation/{wellbore_id}")
def correlation(
    wellbore_id: int, datum: str | None = None, conn: psycopg.Connection = Conn
) -> dict[str, Any]:
    """Depth tracks for the active well and its offsets (latest selection, else the nearest
    wells), each with tops, events (with their source page), casing shoes, mud weights, ECDs
    and LOT/FIT; depths in MD and TVDSS. The client flattens on `datum`. The active track also
    carries its prognosed tops, the roadmap's hazard bands and the bit depth."""
    head = conn.execute(
        "SELECT w.id, w.name, b.td_md FROM wellbore b JOIN well w ON w.id = b.well_id"
        " WHERE b.id = %s",
        (wellbore_id,),
    ).fetchone()
    if head is None:
        raise HTTPException(404, "wellbore not found")
    well_id, name, td = head
    sel = _one(
        conn,
        "SELECT id FROM offset_selection WHERE active_wellbore_id = %s ORDER BY id DESC LIMIT 1",
        (wellbore_id,),
    )
    selected = an.selected_offsets(conn, int(sel)) if sel else []
    weights = selected
    if not weights:  # no selection yet: the nearest wells, for context only (no roadmap)
        weights = [
            (int(r[0]), 0.0)
            for r in conn.execute(
                "SELECT w.id FROM well w, well a WHERE a.id = %s AND w.id <> a.id"
                " ORDER BY ST_Distance(w.surface::geography, a.surface::geography) LIMIT 6",
                (well_id,),
            )
        ]
    active = _track(conn, wellbore_id, name, td)
    tv = _tvdss_of(conn, wellbore_id)
    try:
        forms = repo.active_formations(conn, wellbore_id, None)
    except LookupError:
        forms = []
    active["prognosis"] = [
        {"formation": f.formation, "md": f.top_md, "tvdss": tv(f.top_md)}
        for f in forms
        if f.top_md is not None
    ]
    bands = []
    if selected and forms:
        offs = repo.offsets(conn, selected)
        for c in hz.roadmap(forms, offs, basin_rates=repo.basin_rates(conn)):
            if c.band_md and c.mean >= 0.05:
                bands.append(
                    {
                        "hazard": c.hazard,
                        "formation": c.formation,
                        "mean": round(c.mean, 3),
                        "ci90": [round(x, 3) for x in c.ci90],
                        "md": [round(x, 1) for x in c.band_md],
                        "tvdss": [tv(c.band_md[0]), tv(c.band_md[1])],
                    }
                )
    active["bands"] = bands
    bit = _one(
        conn,
        "SELECT coalesce(hole_md, bit_md) FROM rt_measurement WHERE wellbore_id = %s"
        " ORDER BY ts DESC LIMIT 1",
        (wellbore_id,),
    )
    active["bit"] = None if bit is None else {"md": round(bit, 1), "tvdss": tv(bit)}
    tracks = [active]
    for wid, weight in weights:
        bore = _main_bore(conn, wid)
        row = conn.execute(
            "SELECT w.name, b.td_md FROM wellbore b JOIN well w ON w.id = b.well_id"
            " WHERE b.id = %s",
            (bore,),
        ).fetchone()
        if bore is None or row is None:
            continue
        t = _track(conn, bore, row[0], row[1])
        t["weight"] = round(weight, 3)
        tracks.append(t)
    order = [f.formation for f in forms] or sorted(
        {t["formation"] for tr in tracks for t in tr["tops"]},
        key=lambda f: min(t["md"] for tr in tracks for t in tr["tops"] if t["formation"] == f),
    )
    common = [
        f
        for f in order
        if all(
            any(t["formation"] == f for t in tr["tops"] + tr.get("prognosis", [])) for tr in tracks
        )
    ]
    if datum is not None and datum not in order:
        raise HTTPException(422, f"unknown datum formation {datum!r}; one of {order}")
    target = _one(
        conn,
        "SELECT f.name FROM offset_selection s JOIN formation f ON f.id = s.formation_id"
        " WHERE s.id = %s",
        (sel,),
    )
    default = target if target in common else (common[len(common) // 2] if common else None)
    return {
        "wellbore_id": wellbore_id,
        "selection_id": sel,
        "formations": order,
        "datum": datum or default,
        "datum_choices": common,
        "tracks": tracks,
    }


# ------------------------------------------------------------------ live console


@router.get("/active/{wellbore_id}/history")
def history(
    wellbore_id: int,
    minutes: float = Query(60, gt=0, le=24 * 60),
    max_points: int = Query(1500, ge=10, le=10_000),
    conn: psycopg.Connection = Conn,
) -> dict[str, Any]:
    """Recent samples (same shape as the stream's), newest last, thinned to `max_points`."""
    if _one(conn, "SELECT id FROM wellbore WHERE id = %s", (wellbore_id,)) is None:
        raise HTTPException(404, "wellbore not found")
    last = _one(conn, "SELECT max(ts) FROM rt_measurement WHERE wellbore_id = %s", (wellbore_id,))
    if last is None:
        return {"wellbore_id": wellbore_id, "samples": []}
    typed = [(name, col) for name, (_, col) in CHANNELS.items() if col] + [("ecd", "ecd")]
    cols = ", ".join(c for _, c in typed)
    rows = conn.execute(
        f"SELECT ts, rig_state::text, {cols}, channels FROM rt_measurement"
        " WHERE wellbore_id = %s AND ts > %s ORDER BY ts",
        (wellbore_id, last - timedelta(minutes=minutes)),
    ).fetchall()
    step = max(1, math.ceil(len(rows) / max_points))  # ponytail: stride, not min/max buckets
    samples = []
    for r in rows[::step]:
        values = {n: r[2 + i] for i, (n, _) in enumerate(typed) if r[2 + i] is not None}
        values.update({k: v for k, v in (r[-1] or {}).items() if isinstance(v, int | float)})
        samples.append({"ts": r[0], "rig_state": r[1], "values": values})
    return {"wellbore_id": wellbore_id, "samples": samples}


@router.get("/active/{wellbore_id}/envelope")
def envelope(wellbore_id: int, conn: psycopg.Connection = Conn) -> dict[str, Any]:
    """Two envelopes for the live console.

    Torque and drag: expected hookload against bit depth tripping out, tripping in and rotating
    (Johancsik soft string, this well's survey and mud weight), at the calibrated friction factor
    and +/- 0.1 around it.

    MW/ECD window per formation from offset evidence: kicks bound pore pressure from below (the
    mud weight that did not hold), losses (ECD at which returns were lost) and LOT/FIT bound the
    fracture gradient from above. Each bound lists the records it came from."""
    td = _one(conn, "SELECT td_md FROM wellbore WHERE id = %s", (wellbore_id,))
    if td is None:
        raise HTTPException(404, "wellbore not found")
    ctx = rtctx.load(conn, wellbore_id)
    depths = [float(d) for d in np.arange(0, float(td) + 1, max(25.0, float(td) / 120))]

    def curve(mu: float, mode: str) -> list[float]:
        return [round(torque_drag(ctx.traj, d, ctx.mw_sg, mu, mode)[0], 1) for d in depths]

    mu = ctx.mu
    td_env = {
        "mu": mu,
        "mu_source": ctx.mu_source,
        "mw_sg": ctx.mw_sg,
        "depth_md": [round(d, 1) for d in depths],
        "pickup": curve(mu, "pickup"),
        "slackoff": curve(mu, "slackoff"),
        "rotating": curve(mu, "rotating"),
        "pickup_hi": curve(min(mu + 0.1, 0.6), "pickup"),
        "slackoff_hi": curve(min(mu + 0.1, 0.6), "slackoff"),
    }
    sel = _one(
        conn,
        "SELECT id FROM offset_selection WHERE active_wellbore_id = %s ORDER BY id DESC LIMIT 1",
        (wellbore_id,),
    )
    try:
        forms = repo.active_formations(conn, wellbore_id, None)
    except LookupError:
        forms = []
    offs = [w for w, _ in an.selected_offsets(conn, int(sel))] if sel else []
    ev = repo.evidence(conn, offs) if offs else []
    window = []
    for f in forms:
        here = [e for e in ev if e["formation"] == f.formation and e["value"] is not None]
        pore = [e for e in here if e["kind"] == "kick"]
        frac = [e for e in here if e["kind"] in ("loss", "lot", "fit")]
        window.append(
            {
                "formation": f.formation,
                "top_md": f.top_md,
                "base_md": f.base_md,
                "pore_min_sg": max((e["value"] for e in pore), default=None),
                "frac_max_sg": min((e["value"] for e in frac), default=None),
                "pore_evidence": pore,
                "frac_evidence": frac,
            }
        )
    return {"wellbore_id": wellbore_id, "torque_drag": td_env, "pressure_window": window}


# ------------------------------------------------------------------ evidence files


@router.get("/source-refs/{source_ref_id}")
def source_ref(source_ref_id: int, conn: psycopg.Connection = Conn) -> dict[str, Any]:
    """Resolve a citation's source: document page and box, or voice-note time span."""
    cur = conn.execute(
        "SELECT r.id, r.document_id, d.title AS document, r.page, r.bbox, r.audio_note_id,"
        " r.t_start, r.t_end, r.confidence, r.review_status::text AS review_status,"
        " coalesce(d.synthetic, a.synthetic) AS synthetic FROM source_ref r"
        " LEFT JOIN document d ON d.id = r.document_id"
        " LEFT JOIN audio_note a ON a.id = r.audio_note_id WHERE r.id = %s",
        (source_ref_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "source not found")
    return dict(zip([c.name for c in cur.description or []], row, strict=True))


@router.get("/documents/{document_id}/file")
def document_file(
    document_id: int, user: User = CurrentUser, conn: psycopg.Connection = Conn
) -> Response:
    uri = _one(conn, "SELECT uri FROM document WHERE id = %s", (document_id,))
    if uri is None:
        raise HTTPException(404, "document not found")
    try:
        data = storage.read(str(uri))
    except storage.Missing as e:
        raise HTTPException(404, "no stored file for this document") from e
    except Exception as e:  # storage unreachable
        raise HTTPException(502, f"could not read the file: {type(e).__name__}") from e
    audit.record(conn, user.name, "read.document", "document", document_id)
    return Response(
        data, media_type="application/pdf", headers={"Cache-Control": "private, max-age=3600"}
    )


FIELDS_SQL = """
SELECT 'formation_top', f.name, t.md, 'm MD', r.page, r.bbox, r.confidence, r.review_status::text,
       r.id, t.wellbore_id FROM formation_top t JOIN formation f ON f.id = t.formation_id
       JOIN source_ref r ON r.id = t.source_ref_id WHERE r.document_id = %(d)s
UNION ALL
SELECT 'event:' || e.type::text, coalesce(f.name, ''), e.md_top, 'm MD', r.page, r.bbox,
       r.confidence, r.review_status::text, r.id, e.wellbore_id FROM event e
       LEFT JOIN formation f ON f.id = e.formation_id JOIN source_ref r ON r.id = e.source_ref_id
       WHERE r.document_id = %(d)s
UNION ALL
SELECT 'casing_shoe', c.od_in::text || ' in', c.md_shoe, 'm MD', r.page, r.bbox, r.confidence,
       r.review_status::text, r.id, c.wellbore_id FROM casing_string c
       JOIN source_ref r ON r.id = c.source_ref_id WHERE r.document_id = %(d)s
UNION ALL
SELECT 'mud_weight', coalesce(m.mud_type, ''), m.mw, 'sg', r.page, r.bbox, r.confidence,
       r.review_status::text, r.id, m.wellbore_id FROM mud_record m
       JOIN source_ref r ON r.id = m.source_ref_id WHERE r.document_id = %(d)s
UNION ALL
SELECT l.test_type, '', l.emw, 'sg', r.page, r.bbox, r.confidence, r.review_status::text, r.id,
       l.wellbore_id FROM lot_fit l JOIN source_ref r ON r.id = l.source_ref_id
       WHERE r.document_id = %(d)s
ORDER BY 5, 1, 3
"""


@router.get("/documents/{document_id}/fields")
def document_fields(document_id: int, conn: psycopg.Connection = Conn) -> dict[str, Any]:
    """The document and every fact stored from it (page, box, confidence, review status), plus
    its open review items: what the evidence viewer lists beside the page."""
    doc = conn.execute(
        "SELECT title, type::text, page_count, vintage, synthetic FROM document WHERE id = %s",
        (document_id,),
    ).fetchone()
    if doc is None:
        raise HTTPException(404, "document not found")
    conversions: dict[int, list[dict[str, Any]]] = dict(
        conn.execute(
            "SELECT id, conversions FROM source_ref WHERE document_id = %s", (document_id,)
        ).fetchall()
    )
    facts = [
        {
            "field": kind,
            "label": label,
            "value": None if value is None else round(float(value), 3),
            "unit": unit,
            "page": page,
            "bbox": bbox,
            "confidence": conf,
            "review_status": rs,
            "source_ref_id": sref,
            "wellbore_id": bore,
            # printed -> canonical (units, datum), as the canonical layer recorded it
            "conversions": conversions.get(sref) or [],
        }
        for kind, label, value, unit, page, bbox, conf, rs, sref, bore in conn.execute(
            FIELDS_SQL, {"d": document_id}
        )
    ]
    review = [
        {"id": i, "field": f, "value": v, "printed": p, "page": pg, "bbox": b, "confidence": c}
        for i, f, v, p, pg, b, c in conn.execute(
            "SELECT id, field, value, printed, page, bbox, confidence FROM review_item"
            " WHERE document_id = %s AND status = 'open' ORDER BY page, id",
            (document_id,),
        )
    ]
    return {
        "document_id": document_id,
        "title": doc[0],
        "type": doc[1],
        "pages": doc[2],
        "vintage": doc[3],
        "synthetic": doc[4],
        "facts": facts,
        "review": review,
    }


@router.get("/audio-notes/{note_id}")
def audio_note(note_id: int, conn: psycopg.Connection = Conn) -> dict[str, Any]:
    """A voice note and the spans cited from it (lessons, facts), with their transcript text."""
    row = conn.execute(
        "SELECT a.uri, a.duration_s, a.speaker, a.lang, a.recorded_at, a.note, f.name, w.name"
        " FROM audio_note a LEFT JOIN formation f ON f.id = a.formation_id"
        " LEFT JOIN wellbore b ON b.id = a.wellbore_id LEFT JOIN well w ON w.id = b.well_id"
        " WHERE a.id = %s",
        (note_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "voice note not found")
    spans = [
        {"source_ref_id": i, "t_start": a, "t_end": b, "text": t, "review_status": rs}
        for i, a, b, t, rs in conn.execute(
            "SELECT r.id, r.t_start, r.t_end, (SELECT string_agg(c.text, ' ') FROM chunk c"
            " WHERE c.source_ref_id = r.id), r.review_status::text FROM source_ref r"
            " WHERE r.audio_note_id = %s ORDER BY r.t_start NULLS LAST",
            (note_id,),
        )
    ]
    return {
        "id": note_id,
        "uri": row[0],
        "duration_s": row[1],
        "speaker": row[2],
        "lang": row[3],
        "recorded_at": row[4],
        "note": row[5],
        "formation": row[6],
        "well": row[7],
        "spans": spans,
    }


@router.get("/audio-notes/{note_id}/file")
def audio_file(note_id: int, user: User = CurrentUser, conn: psycopg.Connection = Conn) -> Response:
    uri = _one(conn, "SELECT uri FROM audio_note WHERE id = %s", (note_id,))
    if uri is None:
        raise HTTPException(404, "voice note not found")
    try:
        data = storage.read(str(uri))
    except storage.Missing as e:
        raise HTTPException(404, "no stored recording for this note") from e
    except Exception as e:
        raise HTTPException(502, f"could not read the recording: {type(e).__name__}") from e
    audit.record(conn, user.name, "read.recording", "audio_note", note_id)
    ext = str(uri).rsplit(".", 1)[-1].lower()
    media = {"wav": "audio/wav", "webm": "audio/webm", "ogg": "audio/ogg", "mp3": "audio/mpeg"}
    return Response(data, media_type=media.get(ext, "application/octet-stream"))


# ------------------------------------------------------------------ fleet


@router.get("/fleet")
def fleet(
    digest_hours: float = Query(12, gt=0, le=168), conn: psycopg.Connection = Conn
) -> dict[str, Any]:
    """Per wellbore with a feed or an offset selection: freshness, depth, rate, the next hazard
    band ahead of the bit with its ETA, open alerts by tier. Plus the shift digest: alerts
    raised, acknowledged, cleared and shelved in the last `digest_hours`."""
    now = datetime.now(UTC)
    rigs = []
    for bore, well, block, state, last_ts, last_rx, conn_name in conn.execute(
        "SELECT b.id, w.name, w.block_id, f.state, f.last_sample_ts, f.last_rx_at, f.connector"
        " FROM wellbore b JOIN well w ON w.id = b.well_id"
        " LEFT JOIN rt_feed f ON f.wellbore_id = b.id"
        " WHERE f.wellbore_id IS NOT NULL OR EXISTS"
        " (SELECT 1 FROM offset_selection s WHERE s.active_wellbore_id = b.id)"
        " ORDER BY w.name"
    ).fetchall():
        alerts = {
            int(t): int(n)
            for t, n in conn.execute(
                "SELECT tier, count(*) FROM alert WHERE wellbore_id = %s AND status = 'open'"
                " AND suppressed IS NULL GROUP BY tier",
                (bore,),
            )
        }
        md, rate, basis, as_of = repo.progress(conn, bore)
        nxt = None
        sel = _one(
            conn,
            "SELECT id FROM offset_selection WHERE active_wellbore_id = %s ORDER BY id DESC"
            " LIMIT 1",
            (bore,),
        )
        if sel:
            try:
                forms = repo.active_formations(conn, bore, None)
                offs = repo.offsets(conn, an.selected_offsets(conn, int(sel)))
                cells = hz.roadmap(forms, offs, basin_rates=repo.basin_rates(conn))
                la = hz.lookahead(cells, md, rate, basis, as_of, 0.1)
                nxt = next((b for b in la.bands if b.get("state") in ("in band", "ahead")), None)
            except LookupError:
                nxt = None
        rigs.append(
            {
                "wellbore_id": bore,
                "well": well,
                "block": block,
                "feed_state": state or "none",
                "connector": conn_name,
                "last_sample_ts": last_ts,
                # Freshness is when the last sample arrived (a replay's samples carry history
                # time), as for the live view's stale check.
                "age_s": None
                if (last_rx or last_ts) is None
                else round((now - (last_rx or last_ts)).total_seconds()),
                "current_md": md,
                "rate_m_per_hr": rate,
                "rate_basis": basis,
                "next_hazard": nxt,
                "alerts": {
                    "advisory": alerts.get(1, 0),
                    "warning": alerts.get(2, 0),
                    "critical": alerts.get(3, 0),
                },
            }
        )
    since = now - timedelta(hours=digest_hours)
    digest_rows = conn.execute(
        "SELECT a.id, w.name, a.hazard::text, a.tier, a.status::text, a.raised_at, a.cleared_at,"
        " a.ack_by, a.suppressed, a.peak_posterior FROM alert a"
        " JOIN wellbore b ON b.id = a.wellbore_id JOIN well w ON w.id = b.well_id"
        " WHERE a.raised_at >= %s OR a.cleared_at >= %s ORDER BY a.raised_at DESC",
        (since, since),
    ).fetchall()
    items = [
        {
            "id": i,
            "well": w,
            "hazard": h,
            "tier": t,
            "status": s,
            "raised_at": r,
            "cleared_at": c,
            "ack_by": ack,
            "suppressed": sup,
            "peak_posterior": p,
        }
        for i, w, h, t, s, r, c, ack, sup, p in digest_rows
    ]
    return {
        "as_of": now,
        "rigs": rigs,
        "digest": {
            "hours": digest_hours,
            "raised": sum(1 for x in items if x["raised_at"] and x["raised_at"] >= since),
            "acknowledged": sum(1 for x in items if x["ack_by"]),
            "cleared": sum(1 for x in items if x["cleared_at"] and x["cleared_at"] >= since),
            "shelved": sum(1 for x in items if x["suppressed"] == "shelved"),
            "by_tier": {
                n: sum(1 for x in items if x["tier"] == t)
                for t, n in ((1, "advisory"), (2, "warning"), (3, "critical"))
            },
            "items": items[:50],
        },
    }


# ------------------------------------------------------------------ voice capture


@router.post("/voice-notes", status_code=201)
async def upload_voice_note(
    request: Request,
    wellbore_id: int,
    client_id: str = Query(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
    duration_s: float = Query(gt=0, le=1800),
    formation: str | None = None,
    note: str | None = Query(None, max_length=500),
    recorded_at: datetime | None = None,
    user: User = CurrentUser,
    conn: psycopg.Connection = Conn,
) -> dict[str, Any]:
    """A recording captured in the app (raw audio body), tagged with well and formation.
    Idempotent on `client_id`, so a device can retry after going offline. The pipeline
    transcribes it and drafts lessons; nothing from it is a fact until an expert confirms."""
    user.require("operate")
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip()
    if ctype not in AUDIO_TYPES:
        raise HTTPException(415, f"unsupported audio type {ctype!r}")
    existing = _one(conn, "SELECT id FROM audio_note WHERE client_id = %s", (client_id,))
    if existing is not None:
        return {"id": existing, "status": "pending_confirmation", "duplicate": True}
    row = conn.execute(
        "SELECT w.block_id, w.jv_id FROM wellbore b JOIN well w ON w.id = b.well_id"
        " WHERE b.id = %s",
        (wellbore_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "wellbore not found")
    fid = _formation_id(conn, formation)[0] if formation else None
    data = await request.body()
    if not data:
        raise HTTPException(422, "empty recording")
    if len(data) > MAX_AUDIO:
        raise HTTPException(413, "recording larger than 20 MB")
    sha = hashlib.sha256(data).digest()
    ext = ctype.split("/")[1].replace("x-", "").replace("mpeg", "mp3").replace("mp4", "m4a")
    try:
        uri = storage.put("raw", f"voice/{sha.hex()}.{ext}", data, ctype)
    except Exception as e:
        raise HTTPException(502, f"could not store the recording: {type(e).__name__}") from e
    nid = _one(
        conn,
        "INSERT INTO audio_note (sha256, uri, duration_s, speaker, lang, recorded_at, block_id,"
        " jv_id, wellbore_id, formation_id, note, uploaded_by, client_id, source)"
        " VALUES (%s, %s, %s, %s, 'en', %s, %s, %s, %s, %s, %s, %s, %s, 'upload')"
        " ON CONFLICT (sha256) DO UPDATE SET client_id = coalesce(audio_note.client_id,"
        " EXCLUDED.client_id) RETURNING id",
        (
            sha,
            uri,
            duration_s,
            user.name,
            recorded_at or datetime.now(UTC),
            row[0],
            row[1],
            wellbore_id,
            fid,
            note,
            user.name,
            client_id,
        ),
    )
    audit.record(
        conn,
        user.name,
        "voice_note.upload",
        "audio_note",
        nid,
        {"wellbore_id": wellbore_id, "uri": uri},
    )
    return {"id": nid, "status": "pending_confirmation", "duplicate": False}


@router.get("/voice-notes")
def voice_notes(user: User = CurrentUser, conn: psycopg.Connection = Conn) -> list[dict[str, Any]]:
    """The caller's captured notes. Status: transcribing (no transcript yet), then
    pending_confirmation until an expert approves a lesson from it (confirmed) or resolves all
    its review items without one (reviewed)."""
    out = []
    for nid, w, f, note, dur, rec, n_src, n_open, n_lessons, n_approved in conn.execute(
        "SELECT a.id, wl.name, f.name, a.note, a.duration_s, a.recorded_at,"
        " (SELECT count(*) FROM source_ref r WHERE r.audio_note_id = a.id),"
        " (SELECT count(*) FROM review_item i WHERE i.audio_note_id = a.id"
        "   AND i.status = 'open'),"
        " (SELECT count(*) FROM lesson l JOIN source_ref r ON r.id = l.source_ref_id"
        "   WHERE r.audio_note_id = a.id),"
        " (SELECT count(*) FROM lesson l JOIN source_ref r ON r.id = l.source_ref_id"
        "   WHERE r.audio_note_id = a.id AND l.status = 'approved')"
        " FROM audio_note a LEFT JOIN wellbore b ON b.id = a.wellbore_id"
        " LEFT JOIN well wl ON wl.id = b.well_id LEFT JOIN formation f ON f.id = a.formation_id"
        " WHERE a.uploaded_by = %s ORDER BY a.recorded_at DESC NULLS LAST, a.id DESC LIMIT 100",
        (user.name,),
    ):
        if n_approved:
            status = "confirmed"
        elif n_src == 0 and n_open == 0:
            status = "transcribing"
        elif n_open or n_lessons:
            status = "pending_confirmation"
        else:
            status = "reviewed"
        out.append(
            {
                "id": nid,
                "well": w,
                "formation": f,
                "note": note,
                "duration_s": dur,
                "recorded_at": rec,
                "status": status,
                "lessons": n_lessons,
                "open_review_items": n_open,
            }
        )
    return out
