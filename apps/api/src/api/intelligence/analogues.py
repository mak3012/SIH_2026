"""Analogue (offset) wells for an active wellbore.

Candidates must lie within a SURFACE radius of the active well AND within a SUBSURFACE radius
measured where the two trajectories cross the target formation's top (PostGIS: the crossing
point is interpolated between the survey stations around the top's MD). Deviated wells from one
pad can be kilometres apart at the target, and wells far apart at surface can meet there.

Each candidate gets a transparent score: a weighted mean of factor scores in [0, 1],

    score = sum_f (w_f * s_f) / sum_f w_f

with every factor's raw feature, score, weight and contribution returned. A factor that cannot
be evaluated (e.g. no fault-block interpretation) scores 0.5 and is flagged `unknown`, so missing
data neither helps nor hurts silently. Users pin or exclude wells and tune weights; each choice
is stored with the full feature table as training data for learning-to-rank.
"""

from dataclasses import asdict, dataclass, field
from math import exp
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

DEFAULT_WEIGHTS: dict[str, float] = {
    "distance_at_formation": 3.0,
    "fault_block": 2.0,
    "top_depth_delta": 2.0,
    "design": 1.0,
    "mud_system": 1.0,
    "vintage": 0.5,
    "data_quality": 1.0,
}
# Length scales of the exponential decays (score = exp(-x / scale)).
SCALES = {"distance_m": 3000.0, "top_delta_m": 100.0, "vintage_yr": 15.0}
UNKNOWN_SCORE = 0.5


@dataclass
class Features:
    well_id: int
    well: str
    block: str
    fault_block: str | None
    spud_year: int | None
    surface_m: float
    formation_m: float | None  # distance where both trajectories cross the target top
    top_tvdss: float | None
    casing_ods: list[float] = field(default_factory=list)
    mud_types: list[str] = field(default_factory=list)
    data_quality: float | None = None
    quality_parts: dict[str, float] = field(default_factory=dict)


@dataclass
class Active:
    wellbore_id: int
    well_id: int
    fault_block: str | None
    formation_id: int
    top_tvdss: float | None  # prognosed or picked
    casing_ods: list[float]
    mud_type: str | None
    spud_year: int | None


@dataclass
class Factor:
    name: str
    weight: float
    raw: Any
    score: float
    unknown: bool
    contribution: float
    why: str


@dataclass
class Ranked:
    features: Features
    factors: list[Factor]
    score: float
    rank: int
    within_radii: bool
    pinned: bool
    excluded: bool
    included: bool


def factor_scores(a: Active, c: Features) -> dict[str, tuple[Any, float | None, str]]:
    """(raw feature, score or None when unknown, explanation) per factor."""
    out: dict[str, tuple[Any, float | None, str]] = {}
    if c.formation_m is not None:
        s = exp(-c.formation_m / SCALES["distance_m"])
        out["distance_at_formation"] = (
            round(c.formation_m),
            s,
            f"exp(-{c.formation_m:.0f} m / {SCALES['distance_m']:.0f} m)",
        )
    else:
        out["distance_at_formation"] = (None, None, "no crossing point (no top or survey)")
    if a.fault_block is None or c.fault_block is None:
        out["fault_block"] = (c.fault_block, None, "fault block not interpreted")
    else:
        same = a.fault_block == c.fault_block
        out["fault_block"] = (
            c.fault_block,
            1.0 if same else 0.0,
            "same fault block" if same else f"different ({c.fault_block})",
        )
    if a.top_tvdss is None or c.top_tvdss is None:
        out["top_depth_delta"] = (None, None, "target top missing in one well")
    else:
        d = abs(c.top_tvdss - a.top_tvdss)
        out["top_depth_delta"] = (
            round(d, 1),
            exp(-d / SCALES["top_delta_m"]),
            f"exp(-{d:.1f} m / {SCALES['top_delta_m']:.0f} m)",
        )
    if not a.casing_ods or not c.casing_ods:
        out["design"] = (c.casing_ods, None, "casing design missing in one well")
    else:
        sa, sc = {round(x, 3) for x in a.casing_ods}, {round(x, 3) for x in c.casing_ods}
        j = len(sa & sc) / len(sa | sc)
        out["design"] = (sorted(sc), j, f"Jaccard of casing sizes = {j:.2f}")
    if a.mud_type is None or not c.mud_types:
        out["mud_system"] = (c.mud_types, None, "mud system not known in one well")
    else:
        hit = a.mud_type in c.mud_types
        out["mud_system"] = (
            c.mud_types,
            1.0 if hit else 0.0,
            f"{'same' if hit else 'different'} mud system ({', '.join(c.mud_types)})",
        )
    if a.spud_year is None or c.spud_year is None:
        out["vintage"] = (c.spud_year, None, "spud year unknown")
    else:
        dy = abs(a.spud_year - c.spud_year)
        out["vintage"] = (
            c.spud_year,
            exp(-dy / SCALES["vintage_yr"]),
            f"exp(-{dy} yr / {SCALES['vintage_yr']:.0f} yr)",
        )
    if c.data_quality is None:
        out["data_quality"] = (None, None, "no facts")
    else:
        parts = ", ".join(f"{k} {v:.2f}" for k, v in c.quality_parts.items())
        out["data_quality"] = (round(c.data_quality, 3), c.data_quality, f"mean of {parts}")
    return out


def score(a: Active, c: Features, weights: dict[str, float]) -> tuple[float, list[Factor]]:
    w = {k: max(0.0, float(weights.get(k, DEFAULT_WEIGHTS[k]))) for k in DEFAULT_WEIGHTS}
    total = sum(w.values()) or 1.0
    factors = []
    for name, (raw, s, why) in factor_scores(a, c).items():
        unknown = s is None
        val = UNKNOWN_SCORE if s is None else min(1.0, max(0.0, s))
        factors.append(
            Factor(name, w[name], raw, round(val, 4), unknown, round(w[name] * val / total, 4), why)
        )
    return round(sum(f.weight * f.score for f in factors) / total, 4), factors


def rank(
    a: Active,
    cands: list[Features],
    weights: dict[str, float],
    *,
    subsurface_m: float,
    pinned: set[int],
    excluded: set[int],
    max_offsets: int = 10,
) -> list[Ranked]:
    """Score, rank, and decide inclusion: pinned always, excluded never, the rest by rank among
    wells inside both radii (up to max_offsets)."""
    rows = []
    for c in cands:
        s, fs = score(a, c, weights)
        inside = c.formation_m is not None and c.formation_m <= subsurface_m
        rows.append((s, c, fs, inside))
    rows.sort(key=lambda r: (r[1].well_id not in pinned, -r[0], r[1].well))
    out, taken = [], 0
    for i, (s, c, fs, inside) in enumerate(rows, start=1):
        pin, exc = c.well_id in pinned, c.well_id in excluded
        inc = pin or (not exc and inside and taken < max_offsets)
        taken += inc and not pin
        out.append(Ranked(c, fs, s, i, inside, pin, exc, inc))
    return out


# ------------------------------------------------------------------ database


CROSSING = """
    SELECT DISTINCT ON (t.wellbore_id) t.wellbore_id, t.tvdss,
        CASE WHEN s2.md IS NULL OR s2.md = s1.md THEN s1.geom
             ELSE ST_LineInterpolatePoint(ST_MakeLine(s1.geom, s2.geom),
                                          (t.md - s1.md) / (s2.md - s1.md)) END AS geom
    FROM formation_top t
    JOIN LATERAL (SELECT md, geom FROM survey_station
                  WHERE wellbore_id = t.wellbore_id AND md <= t.md
                  ORDER BY md DESC LIMIT 1) s1 ON true
    LEFT JOIN LATERAL (SELECT md, geom FROM survey_station
                       WHERE wellbore_id = t.wellbore_id AND md >= t.md
                       ORDER BY md LIMIT 1) s2 ON true
    WHERE t.formation_id = %(fid)s
    ORDER BY t.wellbore_id, t.id DESC
"""


def active_context(
    conn: psycopg.Connection,
    wellbore_id: int,
    formation_id: int,
    program: dict[str, Any] | None = None,
) -> Active:
    row = conn.execute(
        "SELECT w.id, w.fault_block, extract(year FROM coalesce(w.spud_date, now()))::int"
        " FROM wellbore b JOIN well w ON w.id = b.well_id WHERE b.id = %s",
        (wellbore_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"wellbore {wellbore_id} not found")
    top = conn.execute(
        "SELECT tvdss FROM formation_top WHERE wellbore_id = %s AND formation_id = %s"
        " ORDER BY id DESC LIMIT 1",
        (wellbore_id, formation_id),
    ).fetchone()
    ods = [
        float(r[0])
        for r in conn.execute(
            "SELECT od_in FROM casing_string WHERE wellbore_id = %s", (wellbore_id,)
        )
    ]
    mud = None
    if program:
        ods = ods or [
            float(s["casing_od_in"]) for s in program.get("sections", []) if s.get("casing_od_in")
        ]
        mud = next(
            (s.get("mud_type") for s in program.get("sections", []) if s.get("mud_type")), None
        )
    return Active(
        wellbore_id, row[0], row[1], formation_id, top[0] if top else None, ods, mud, row[2]
    )


def candidates(
    conn: psycopg.Connection,
    active: Active,
    *,
    surface_m: float,
    include: frozenset[int] | set[int] = frozenset(),
) -> list[Features]:
    """Wells within the surface radius (plus any pinned ones), with their features."""
    rows = conn.execute(
        f"WITH crossing AS ({CROSSING}),"
        " a AS (SELECT w.id, w.surface, b.id AS bore FROM wellbore b"
        "       JOIN well w ON w.id = b.well_id"
        "       WHERE b.id = %(bore)s)"
        " SELECT w.id, w.name, w.block_id, w.fault_block,"
        "   extract(year FROM w.spud_date)::int,"
        "   ST_Distance(w.surface::geography, a.surface::geography),"
        "   ST_Distance(c.geom::geography, ac.geom::geography), c.tvdss, b.id"
        " FROM a JOIN well w ON w.id <> a.id"
        "   AND (ST_DWithin(w.surface::geography, a.surface::geography, %(rs)s)"
        "        OR w.id = ANY(%(inc)s))"
        " JOIN wellbore b ON b.well_id = w.id AND b.parent_id IS NULL"
        " LEFT JOIN crossing c ON c.wellbore_id = b.id"
        " LEFT JOIN crossing ac ON ac.wellbore_id = a.bore"
        " ORDER BY w.id",
        {
            "fid": active.formation_id,
            "bore": active.wellbore_id,
            "rs": surface_m,
            "inc": list(include),
        },
    ).fetchall()
    out = []
    for wid, name, block, fb, year, surf, form, top, bore in rows:
        f = Features(
            wid, name, block, fb, year, float(surf), None if form is None else float(form), top
        )
        f.casing_ods = [
            float(r[0])
            for r in conn.execute("SELECT od_in FROM casing_string WHERE wellbore_id = %s", (bore,))
        ]
        f.mud_types = sorted(
            {
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT mud_type FROM mud_record WHERE wellbore_id = %s"
                    " AND mud_type IS NOT NULL",
                    (bore,),
                )
            }
        )
        f.data_quality, f.quality_parts = quality(conn, bore)
        out.append(f)
    return out


def quality(conn: psycopg.Connection, bore: int) -> tuple[float | None, dict[str, float]]:
    """Mean of: has a survey, has formation tops, has documented drilling problems, and the
    mean confidence of the wellbore's stored facts."""
    has = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM survey_station WHERE wellbore_id = %(b)s),"
        " EXISTS (SELECT 1 FROM formation_top WHERE wellbore_id = %(b)s),"
        " EXISTS (SELECT 1 FROM event_documentation WHERE wellbore_id = %(b)s),"
        " (SELECT avg(r.confidence) FROM source_ref r WHERE r.id IN ("
        "   SELECT source_ref_id FROM formation_top WHERE wellbore_id = %(b)s UNION"
        "   SELECT source_ref_id FROM event WHERE wellbore_id = %(b)s UNION"
        "   SELECT source_ref_id FROM casing_string WHERE wellbore_id = %(b)s))",
        {"b": bore},
    ).fetchone()
    assert has is not None
    parts = {"survey": float(has[0]), "tops": float(has[1]), "problems documented": float(has[2])}
    if has[3] is not None:
        parts["mean confidence"] = float(has[3])
    if not any(has[:3]) and has[3] is None:
        return None, parts
    return sum(parts.values()) / len(parts), parts


def save_selection(
    conn: psycopg.Connection,
    active: Active,
    ranked: list[Ranked],
    params: dict[str, Any],
    user: str,
) -> int:
    """Store the user's choice and every candidate's features for learning-to-rank."""
    row = conn.execute(
        "INSERT INTO offset_selection (active_wellbore_id, formation_id, params, created_by)"
        " VALUES (%s, %s, %s, %s) RETURNING id",
        (active.wellbore_id, active.formation_id, Jsonb(params), user),
    ).fetchone()
    assert row is not None
    for r in ranked:
        conn.execute(
            "INSERT INTO offset_selection_item (selection_id, well_id, features, factors, score,"
            " rank, pinned, excluded, included) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                row[0],
                r.features.well_id,
                Jsonb(asdict(r.features)),
                Jsonb([asdict(f) for f in r.factors]),
                r.score,
                r.rank,
                r.pinned,
                r.excluded,
                r.included,
            ),
        )
    return int(row[0])


def selected_offsets(conn: psycopg.Connection, selection_id: int) -> list[tuple[int, float]]:
    """(well id, similarity weight = score) of the wells a selection included."""
    return [
        (int(w), float(s))
        for w, s in conn.execute(
            "SELECT well_id, score FROM offset_selection_item WHERE selection_id = %s AND included",
            (selection_id,),
        )
    ]
