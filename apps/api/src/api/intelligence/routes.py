"""HTTP endpoints for analogues, hazard roadmap, lookahead, program lint and briefings.

Every request runs in one transaction as app_rw with the caller's blocks and JVs (row-level
security), so offsets and evidence outside the caller's licences never enter a calculation.
Writes (selections, programs, lint runs, briefings) need the `plan` permission (role `planner`).
"""

import os
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from api import audit
from api.auth import CurrentUser, User
from api.db import scoped
from api.intelligence import analogues as an
from api.intelligence import hazards as hz
from api.intelligence import linter as li
from api.intelligence import repo
from core.canonical.equipment import size_label
from core.llm import LLM, LLMConfig, PgLog, PromptSet

router = APIRouter(tags=["intelligence"])
PROMPTS = PromptSet(Path(__file__).resolve().parents[1] / "prompts", {"briefing": "v2"})


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"])


def db(user: User = CurrentUser) -> Iterator[psycopg.Connection]:
    """A connection whose single transaction is scoped to the caller (RLS)."""
    with _connect() as conn, scoped(conn, "app_rw", user.blocks, user.jvs):
        yield conn


Conn = Depends(db)


def llm() -> LLM:
    return LLM(LLMConfig.from_env(), PgLog(_connect), prompts=PROMPTS)


def _formation_id(conn: psycopg.Connection, formation: str | int) -> tuple[int, str]:
    row = conn.execute(
        "SELECT id, name FROM formation WHERE id::text = %s OR lower(name) = lower(%s)"
        " OR upper(%s) = ANY (SELECT upper(s) FROM unnest(synonyms || abbreviations) s)"
        " ORDER BY id LIMIT 1",
        (str(formation), str(formation), str(formation)),
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"formation {formation!r} not found")
    return int(row[0]), str(row[1])


# ------------------------------------------------------------------ wells near a point


@router.get("/wells")
def wells_near(
    near: str = Query(..., description="'lat,lon' (WGS 84) or 'well:<name>'"),
    radius_m: float = Query(5000, gt=0, le=100_000),
    conn: psycopg.Connection = Conn,
) -> list[dict[str, Any]]:
    """Wells whose surface location is within `radius_m` of a point or of another well."""
    if near.startswith("well:"):
        row = conn.execute(
            "SELECT ST_Y(surface), ST_X(surface) FROM well WHERE name = %s", (near[5:],)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"well {near[5:]!r} not found")
        lat, lon = row
    else:
        try:
            lat, lon = (float(x) for x in near.split(","))
        except ValueError as e:
            raise HTTPException(422, "near must be 'lat,lon' or 'well:<name>'") from e
    rows = conn.execute(
        "SELECT id, name, block_id, ST_Y(surface), ST_X(surface), datum_confidence,"
        " position_uncertainty_m, round(ST_Distance(surface::geography,"
        "   ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography)::numeric, 1)"
        " FROM well WHERE ST_DWithin(surface::geography,"
        "   ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography, %(r)s)"
        " ORDER BY 8",
        {"lat": lat, "lon": lon, "r": radius_m},
    ).fetchall()
    keys = [
        "well_id",
        "well",
        "block",
        "lat",
        "lon",
        "datum_confidence",
        "position_uncertainty_m",
        "distance_m",
    ]
    return [dict(zip(keys, (*r[:7], float(r[7])), strict=True)) for r in rows]


@router.get("/wellbores")
def list_wellbores(conn: psycopg.Connection = Conn) -> list[dict[str, Any]]:
    """Every wellbore the caller may see, with what the UI needs to pick one: status, TD,
    whether offsets were selected, real-time feed state and open alerts."""
    rows = conn.execute(
        "SELECT b.id, w.name, b.name, w.block_id, b.status, b.td_md,"
        " EXISTS (SELECT 1 FROM offset_selection s WHERE s.active_wellbore_id = b.id),"
        " (SELECT f.state FROM rt_feed f WHERE f.wellbore_id = b.id ORDER BY f.updated_at DESC"
        "  LIMIT 1),"
        " (SELECT count(*) FROM alert a WHERE a.wellbore_id = b.id AND a.status = 'open'"
        "  AND a.suppressed IS NULL AND a.cleared_at IS NULL),"
        " ST_Y(w.surface), ST_X(w.surface), w.field"
        " FROM wellbore b JOIN well w ON w.id = b.well_id ORDER BY w.name, b.id"
    ).fetchall()
    keys = ["wellbore_id", "well", "wellbore", "block", "status", "td_md", "has_selection",
            "feed_state", "open_alerts", "lat", "lon", "field"]  # fmt: skip
    return [dict(zip(keys, r, strict=True)) for r in rows]


@router.get("/wellbores/{wellbore_id}/formations")
def wellbore_formations(wellbore_id: int, conn: psycopg.Connection = Conn) -> list[dict[str, Any]]:
    """Formations along the wellbore (tops, human picks first): the targets offsets can be
    selected at."""
    if conn.execute("SELECT 1 FROM wellbore WHERE id = %s", (wellbore_id,)).fetchone() is None:
        raise HTTPException(404, "wellbore not found")
    try:
        return [asdict(f) for f in repo.active_formations(conn, wellbore_id, None)]
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


# ------------------------------------------------------------------ analogue selection


class OffsetRequest(BaseModel):
    active_wellbore_id: int
    formation: str = Field(description="target formation: id, name, synonym or abbreviation")
    surface_radius_m: float = Field(10_000, gt=0, le=100_000)
    subsurface_radius_m: float = Field(5_000, gt=0, le=100_000)
    weights: dict[str, float] = {}
    pinned: list[int] = []
    excluded: list[int] = []
    max_offsets: int = Field(10, ge=1, le=100)


@router.post("/offsets/select")
def select_offsets(
    req: OffsetRequest, user: User = CurrentUser, conn: psycopg.Connection = Conn
) -> dict[str, Any]:
    """Rank candidate analogues, apply the user's pins/exclusions/weights, store the choice."""
    user.require("plan")
    unknown = set(req.weights) - set(an.DEFAULT_WEIGHTS)
    if unknown:
        raise HTTPException(
            422, f"unknown weight(s) {sorted(unknown)}; known: {sorted(an.DEFAULT_WEIGHTS)}"
        )
    if set(req.pinned) & set(req.excluded):
        raise HTTPException(422, "a well cannot be both pinned and excluded")
    fid, fname = _formation_id(conn, req.formation)
    try:
        active = an.active_context(conn, req.active_wellbore_id, fid)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    cands = an.candidates(conn, active, surface_m=req.surface_radius_m, include=set(req.pinned))
    ranked = an.rank(
        active,
        cands,
        req.weights,
        subsurface_m=req.subsurface_radius_m,
        pinned=set(req.pinned),
        excluded=set(req.excluded),
        max_offsets=req.max_offsets,
    )
    weights = {**an.DEFAULT_WEIGHTS, **req.weights}
    sel = an.save_selection(
        conn,
        active,
        ranked,
        {**req.model_dump(), "weights": weights, "scales": an.SCALES},
        user.name,
    )
    audit.record(
        conn,
        user.name,
        "offsets.select",
        "offset_selection",
        sel,
        {
            "active_wellbore_id": req.active_wellbore_id,
            "formation": fname,
            "weights": weights,
            "pinned": req.pinned,
            "excluded": req.excluded,
            "included": [r.features.well for r in ranked if r.included],
        },
    )
    return {
        "selection_id": sel,
        "formation": fname,
        "weights": weights,
        "scales": an.SCALES,
        "candidates": [
            {
                "well_id": r.features.well_id,
                "well": r.features.well,
                "rank": r.rank,
                "score": r.score,
                "included": r.included,
                "pinned": r.pinned,
                "excluded": r.excluded,
                "within_radii": r.within_radii,
                "surface_m": round(r.features.surface_m),
                "formation_m": None
                if r.features.formation_m is None
                else round(r.features.formation_m),
                "factors": [asdict(f) for f in r.factors],
            }
            for r in ranked
        ],
    }


def _selection(conn: psycopg.Connection, wellbore_id: int, selection_id: int | None) -> int:
    row = conn.execute(
        "SELECT id FROM offset_selection WHERE active_wellbore_id = %s"
        " AND (%s::bigint IS NULL OR id = %s) ORDER BY id DESC LIMIT 1",
        (wellbore_id, selection_id, selection_id),
    ).fetchone()
    if row is None:
        raise HTTPException(409, "no offset selection for this wellbore: POST /offsets/select")
    return int(row[0])


def _roadmap(
    conn: psycopg.Connection, wellbore_id: int, selection_id: int | None, td_md: float | None = None
) -> tuple[int, list[hz.ActiveFormation], list[hz.Cell], list[hz.OffsetWell]]:
    sel = _selection(conn, wellbore_id, selection_id)
    try:
        forms = repo.active_formations(conn, wellbore_id, td_md)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    offs = repo.offsets(conn, an.selected_offsets(conn, sel))
    return sel, forms, hz.roadmap(forms, offs, basin_rates=repo.basin_rates(conn)), offs


@router.get("/active/{wellbore_id}/hazard-profile")
def hazard_profile(
    wellbore_id: int, selection_id: int | None = None, conn: psycopg.Connection = Conn
) -> dict[str, Any]:
    """Per formation and hazard: posterior probability, 90 % interval, n_eff, counts
    (event / none / unknown / not reached), severity mix, band in this well, mitigations."""
    sel, forms, cells, offs = _roadmap(conn, wellbore_id, selection_id)
    return {
        "selection_id": sel,
        "offsets": [{"well_id": o.well_id, "well": o.well, "weight": o.weight} for o in offs],
        "formations": [asdict(f) for f in forms],
        "cells": hz.cells_json(cells),
        "parameters": {
            "coverage_min": hz.COVERAGE_MIN,
            "prior_strength": hz.PRIOR_STRENGTH,
            "interval": 0.9,
        },
    }


@router.get("/active/{wellbore_id}/lookahead")
def lookahead(
    wellbore_id: int,
    selection_id: int | None = None,
    min_mean: float = Query(0.1, ge=0, le=1),
    window_h: float = Query(12, gt=0, le=168),
    conn: psycopg.Connection = Conn,
) -> dict[str, Any]:
    """Distance and time to each hazard band ahead of the bit, from the rolling ROP."""
    _, _, cells, _ = _roadmap(conn, wellbore_id, selection_id)
    md, rate, basis, as_of = repo.progress(conn, wellbore_id, window_h)
    return asdict(hz.lookahead(cells, md, rate, basis, as_of, min_mean))


# ------------------------------------------------------------------ program lint, briefing


class LintRequest(BaseModel):
    program: li.Program
    selection_id: int | None = None


def _lint(
    conn: psycopg.Connection, p: li.Program, selection_id: int | None
) -> tuple[int, list[li.Finding], li.EvidenceSet, list[hz.Cell]]:
    sel, forms, cells, offs = _roadmap(conn, p.wellbore_id, selection_id, p.td_md)
    ev = li.EvidenceSet(repo.evidence(conn, [o.well_id for o in offs]))
    at = {
        f.formation: (f.top_md, f.base_md)
        for f in forms
        if f.top_md is not None and f.base_md is not None
    }
    return sel, li.lint(p, cells, ev, at), ev, cells


@router.post("/programs/lint")
def lint_program(
    req: LintRequest, user: User = CurrentUser, conn: psycopg.Connection = Conn
) -> dict[str, Any]:
    """Rule findings for a drilling program, each with its numbers and offset evidence."""
    user.require("plan")
    sel, findings, ev, cells = _lint(conn, req.program, req.selection_id)
    prog = conn.execute(
        "INSERT INTO drilling_program (wellbore_id, program, uploaded_by) VALUES (%s, %s, %s)"
        " RETURNING id",
        (req.program.wellbore_id, Jsonb(req.program.model_dump()), user.name),
    ).fetchone()
    assert prog is not None
    run = conn.execute(
        "INSERT INTO lint_run (program_id, selection_id, findings, roadmap, ruleset_version,"
        " created_by) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (
            prog[0],
            sel,
            Jsonb(
                {"findings": li.findings_json(findings), "evidence": [asdict(e) for e in ev.items]}
            ),
            Jsonb(hz.cells_json(cells)),
            li.RULESET,
            user.name,
        ),
    ).fetchone()
    assert run is not None
    audit.record(
        conn,
        user.name,
        "program.lint",
        "lint_run",
        run[0],
        {
            "program_id": prog[0],
            "wellbore_id": req.program.wellbore_id,
            "selection_id": sel,
            "ruleset": li.RULESET,
        },
    )
    return {
        "lint_run_id": run[0],
        "program_id": prog[0],
        "ruleset": li.RULESET,
        "selection_id": sel,
        "findings": li.findings_json(findings),
        "evidence": [asdict(e) for e in ev.items],
        "summary": {s: sum(f.severity == s for f in findings) for s in ("block", "warn", "info")},
    }


class BriefingRequest(BaseModel):
    lint_run_id: int


@router.post("/briefings/{wellbore_id}")
def create_briefing(
    wellbore_id: int,
    req: BriefingRequest,
    user: User = CurrentUser,
    conn: psycopg.Connection = Conn,
) -> dict[str, Any]:
    """LLM-drafted briefing for a lint run, every number and citation verified by rules, with a
    PDF. Falls back to rule-generated text when the draft fails verification."""
    user.require("plan")
    row = conn.execute(
        "SELECT l.findings, l.roadmap, p.program FROM lint_run l"
        " JOIN drilling_program p ON p.id = l.program_id WHERE l.id = %s AND p.wellbore_id = %s",
        (req.lint_run_id, wellbore_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "lint run not found for this wellbore")
    stored, cells_raw, program = row
    findings = [li.Finding(**f) for f in stored["findings"]]
    ev = li.EvidenceSet([{k: v for k, v in e.items() if k != "id"} for e in stored["evidence"]])
    cells = [
        hz.Cell(
            **{
                **c,
                "ci90": tuple(c["ci90"]),
                "band_md": tuple(c["band_md"]) if c["band_md"] else None,
                "band_tvdss": tuple(c["band_tvdss"]) if c["band_tvdss"] else None,
                "prior": tuple(c["prior"]),
            }
        )
        for c in cells_raw
    ]
    well = repo.well_name(conn, wellbore_id)
    protected = sorted(
        {e.well for e in ev.items}
        | {well}
        | {s["name"] for s in program["sections"]}
        | {size_label(s["hole_in"]) for s in program["sections"]}
        | {size_label(s["casing_od_in"]) for s in program["sections"] if s.get("casing_od_in")}
        | {str(e.source.get("document") or "") for e in ev.items} - {""}
    )
    b = li.brief(llm(), well, findings, ev, cells, protected)
    pdf = li.pdf(b, findings, ev, user.name)
    out = conn.execute(
        "INSERT INTO briefing (wellbore_id, lint_run_id, content, evidence, verification,"
        " status, model, pdf, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " RETURNING id",
        (
            wellbore_id,
            req.lint_run_id,
            Jsonb(b.draft.model_dump()),
            Jsonb([asdict(e) for e in ev.items]),
            Jsonb({"attempts": b.problems}),
            b.status,
            b.model,
            pdf,
            user.name,
        ),
    ).fetchone()
    assert out is not None
    audit.record(
        conn,
        user.name,
        "briefing.create",
        "briefing",
        out[0],
        {"lint_run_id": req.lint_run_id, "status": b.status, "model": b.model},
    )
    return {
        "briefing_id": out[0],
        "status": b.status,
        "briefing": b.draft.model_dump(),
        "verification": b.problems,
        "model": b.model,
        "pdf_url": f"/briefings/{wellbore_id}/{out[0]}.pdf",
    }


@router.get("/briefings/{wellbore_id}/{briefing_id}.pdf")
def briefing_pdf(
    wellbore_id: int, briefing_id: int, user: User = CurrentUser, conn: psycopg.Connection = Conn
) -> Response:
    row = conn.execute(
        "SELECT pdf FROM briefing WHERE id = %s AND wellbore_id = %s", (briefing_id, wellbore_id)
    ).fetchone()
    if row is None or row[0] is None:
        raise HTTPException(404, "briefing not found")
    audit.record(conn, user.name, "read.briefing", "briefing", briefing_id)
    return Response(
        bytes(row[0]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="briefing-{briefing_id}.pdf"'},
    )
