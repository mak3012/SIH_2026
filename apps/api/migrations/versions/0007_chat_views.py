"""Read-only views for chat text-to-SQL, with a comment on every column.

Views live in schema `chat`, are owned by app_rw (so base-table RLS applies to them with the
caller's session attributes) and are the only thing app_ro can read. Rejected facts are hidden.

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"

VIEWS = {
    "v_wells": """
        SELECT w.id AS well_id, w.name AS well, w.uwi, w.field, w.basin, w.block_id, w.jv_id,
               ST_X(w.surface) AS lon, ST_Y(w.surface) AS lat, w.surface,
               w.rt_elevation_m, w.spud_date, w.status,
               (SELECT count(*) FROM wellbore b WHERE b.well_id = w.id) AS wellbore_count,
               (SELECT max(b.td_md) FROM wellbore b WHERE b.well_id = w.id) AS td_md
        FROM well w""",
    "v_events": """
        SELECT e.id AS event_id, w.id AS well_id, w.name AS well, b.id AS wellbore_id,
               b.name AS wellbore, w.field, w.block_id, e.type::text AS event_type,
               e.md_top, e.md_base, e.tvdss_top, e.tvdss_base,
               f.name AS formation, f.basin, e.rel_pos,
               e.t_start, e.t_end, e.npt_hr, e.severity, e.loss_bbl, e.mw, e.ecd,
               e.narrative, e.root_cause, e.root_cause_status::text AS root_cause_status,
               e.outcome,
               (SELECT string_agg(m.action, '; ' ORDER BY m.t_applied)
                  FROM mitigation m WHERE m.event_id = e.id) AS mitigations,
               d.title AS source_document, r.page AS source_page,
               r.review_status::text AS review_status, e.source_ref_id
        FROM event e
        JOIN wellbore b ON b.id = e.wellbore_id
        JOIN well w ON w.id = b.well_id
        JOIN source_ref r ON r.id = e.source_ref_id
        LEFT JOIN document d ON d.id = r.document_id
        LEFT JOIN formation f ON f.id = e.formation_id
        WHERE r.review_status <> 'rejected'""",
    "v_tops": """
        SELECT t.id AS top_id, w.id AS well_id, w.name AS well, b.id AS wellbore_id,
               b.name AS wellbore, w.block_id, f.name AS formation, f.basin, f.synonyms,
               t.md, t.tvd, t.tvdss, p.geom AS position,
               ST_X(p.geom) AS lon, ST_Y(p.geom) AS lat,
               r.review_status::text AS review_status, t.source_ref_id
        FROM formation_top t
        JOIN wellbore b ON b.id = t.wellbore_id
        JOIN well w ON w.id = b.well_id
        JOIN formation f ON f.id = t.formation_id
        JOIN source_ref r ON r.id = t.source_ref_id
        LEFT JOIN LATERAL (
            SELECT s.geom FROM survey_station s
            WHERE s.wellbore_id = t.wellbore_id
            ORDER BY abs(s.md - t.md) LIMIT 1) p ON true
        WHERE r.review_status <> 'rejected'""",
    "v_casing": """
        SELECT c.id AS casing_id, w.id AS well_id, w.name AS well, b.id AS wellbore_id,
               b.name AS wellbore, w.block_id, c.kind, c.od_in, c.hole_size_in, c.weight_ppf,
               c.grade, c.md_top, c.md_shoe, c.tvdss_shoe, c.set_date,
               lf.test_type AS shoe_test, lf.emw AS shoe_test_emw,
               cj.toc_md, cj.returns AS cement_returns,
               r.review_status::text AS review_status, c.source_ref_id
        FROM casing_string c
        JOIN wellbore b ON b.id = c.wellbore_id
        JOIN well w ON w.id = b.well_id
        JOIN source_ref r ON r.id = c.source_ref_id
        LEFT JOIN LATERAL (
            SELECT x.test_type, x.emw FROM lot_fit x WHERE x.casing_string_id = c.id
            ORDER BY x.test_date DESC NULLS LAST LIMIT 1) lf ON true
        LEFT JOIN LATERAL (
            SELECT j.toc_md, j.returns FROM cement_job j WHERE j.casing_string_id = c.id
            ORDER BY j.stage LIMIT 1) cj ON true
        WHERE r.review_status <> 'rejected'""",
}

_WELL = {
    "well_id": "Well id; join key across views.",
    "well": "Well name as used in reports, e.g. DEMO-1.",
    "wellbore_id": "Wellbore id. A well has one original hole and zero or more sidetracks.",
    "wellbore": "Wellbore name, e.g. DEMO-1 or DEMO-1 ST1 for a sidetrack.",
    "block_id": "Licence block code.",
    "review_status": "Human review state of the source: pending, accepted or corrected.",
    "source_ref_id": "Provenance id; cite this. Resolves to document page + bbox.",
}

COMMENTS = {
    "v_wells": {
        **{k: _WELL[k] for k in ("well_id", "well", "block_id")},
        "uwi": "Unique well identifier, if assigned.",
        "field": "Oil/gas field name.",
        "basin": "Sedimentary basin, e.g. Upper Assam.",
        "jv_id": "Joint venture code, NULL if sole-risk.",
        "lon": "Surface longitude, degrees WGS 84.",
        "lat": "Surface latitude, degrees WGS 84.",
        "surface": "Surface location, geometry(Point, 4326). For distance in metres use "
        "ST_DWithin(a.surface::geography, b.surface::geography, metres).",
        "rt_elevation_m": "Rotary table height above mean sea level, m. TVDSS = TVD - this.",
        "spud_date": "Date drilling started.",
        "status": "Well status, e.g. drilling, producing, abandoned.",
        "wellbore_count": "Number of wellbores (original hole + sidetracks).",
        "td_md": "Deepest total depth of any wellbore, m MD.",
    },
    "v_events": {
        **_WELL,
        "event_id": "Event id.",
        "field": "Oil/gas field name.",
        "event_type": "One of: loss, kick, stuck_pipe, tight_hole, pack_off, torque_spike, "
        "cement_issue, washout, fishing, other.",
        "md_top": "Event top depth, m MD from RT.",
        "md_base": "Event base depth, m MD; NULL for a point event.",
        "tvdss_top": "Event top depth, m TVD below mean sea level.",
        "tvdss_base": "Event base depth, m TVDSS.",
        "formation": "Formation the event occurred in.",
        "basin": "Basin of that formation.",
        "rel_pos": "Position within the formation: 0 = at the top, 1 = at the base.",
        "t_start": "Event start time (UTC).",
        "t_end": "Event end time (UTC).",
        "npt_hr": "Non-productive time caused, hours.",
        "severity": "1 minor ... 5 well-control event.",
        "loss_bbl": "Mud lost to the formation, barrels. Only on loss events.",
        "mw": "Mud weight at the time, specific gravity (sg).",
        "ecd": "Equivalent circulating density at the time, sg.",
        "narrative": "Event description as written in the report.",
        "root_cause": "Root cause text; NULL when unknown.",
        "root_cause_status": "recorded (stated in report), inferred, or unknown.",
        "outcome": "How the event ended, e.g. cured with LCM, cased off, sidetracked.",
        "mitigations": "Actions taken, in order, separated by '; '.",
        "source_document": "Title of the report the event was taken from.",
        "source_page": "Page number in that report.",
    },
    "v_tops": {
        **_WELL,
        "top_id": "Formation top (pick) id.",
        "formation": "Formation name, e.g. Barail. Also match synonyms.",
        "basin": "Basin of the formation.",
        "synonyms": "Alternative names for the formation (text array). "
        "Match with: 'name' = ANY(synonyms).",
        "md": "Top depth, m MD from RT.",
        "tvd": "Top depth, m TVD from RT.",
        "tvdss": "Top depth, m TVD below mean sea level.",
        "position": "Subsurface location of the top, geometry(PointZ, 4326), Z = -TVDSS. "
        "Distance in metres: ST_Distance(a.position::geography, b.position::geography).",
        "lon": "Longitude of the top, degrees WGS 84.",
        "lat": "Latitude of the top, degrees WGS 84.",
    },
    "v_casing": {
        **_WELL,
        "casing_id": "Casing string id.",
        "kind": "conductor, surface, intermediate, production or liner.",
        "od_in": "Casing outside diameter, inches.",
        "hole_size_in": "Hole (bit) size the casing was run in, inches.",
        "weight_ppf": "Nominal weight, lb/ft.",
        "grade": "Steel grade, e.g. N80, P110.",
        "md_top": "Top of the string, m MD (0 for full strings, hanger depth for liners).",
        "md_shoe": "Shoe depth, m MD.",
        "tvdss_shoe": "Shoe depth, m TVDSS.",
        "set_date": "Date the casing was set.",
        "shoe_test": "Latest test below this shoe: lot or fit.",
        "shoe_test_emw": "Equivalent mud weight from that test, sg. Upper limit for ECD.",
        "toc_md": "Top of cement behind this casing, m MD.",
        "cement_returns": "Cement returns at surface: full, partial or none.",
    },
}


def run(sql: str) -> None:
    # Raw driver call: multi-statement SQL, no bind-parameter parsing of ':' or '%'.
    op.get_bind().connection.driver_connection.execute(sql)


def _q(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def upgrade() -> None:
    run("CREATE SCHEMA chat; GRANT USAGE ON SCHEMA chat TO app_ro;")
    run("GRANT CREATE ON SCHEMA chat TO app_rw;")  # needed to own views; revoked below
    for name, body in VIEWS.items():
        run(f"CREATE VIEW chat.{name} AS {body};")
        # Owner app_rw (not the table owner) so RLS filters rows for whoever queries the view.
        run(f"ALTER VIEW chat.{name} OWNER TO app_rw; GRANT SELECT ON chat.{name} TO app_ro;")
        for col, text in COMMENTS[name].items():
            run(f"COMMENT ON COLUMN chat.{name}.{col} IS {_q(text)};")
    run("REVOKE CREATE ON SCHEMA chat FROM app_rw;")
    run("COMMENT ON SCHEMA chat IS 'Read-only views for chat text-to-SQL. Row-filtered by RLS.';")
    run("COMMENT ON VIEW chat.v_wells IS 'One row per well: location, datum, depth.';")
    run(
        "COMMENT ON VIEW chat.v_events IS 'One row per drilling event with well, formation, "
        "mitigations and source page.';"
    )
    run(
        "COMMENT ON VIEW chat.v_tops IS 'One row per formation top pick with its subsurface "
        "position.';"
    )
    run("COMMENT ON VIEW chat.v_casing IS 'One row per casing string with shoe test and cement.';")


def downgrade() -> None:
    run("DROP SCHEMA chat CASCADE;")
