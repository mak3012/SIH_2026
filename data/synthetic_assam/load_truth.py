"""Load synthetic_assam ground truth into Postgres as accepted facts, without OCR.

What the extraction pipeline would store if it read every Well Completion Report perfectly:
wells (with the datum confidence and position uncertainty the canonical CRS step would give the
printed coordinates, and a synthetic fault-block interpretation), wellbores, formation tops,
casing, LOT/FIT, daily mud weights (from the rendered DDRs), drilling problems with
mitigations, each with
a source_ref to the WCR page and bbox where the value is printed (the same layout the PDFs are
rendered with). Plus retrieval passages: the event narrative of each field note (page 1) and
the voice-note script per sentence with its time span.

Used by the assistant evaluation and for demos without OCR. All of it is SYNTHETIC
(document.synthetic = true).

    cd data && uv run python -m synthetic_assam.load_truth --wells 30 --seed 1
"""

import argparse
import hashlib
import math
import os
import random
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from core.canonical.crs import to_wgs84
from synthetic_assam import render
from synthetic_assam.model import Well, build_well, load_config
from synthetic_assam.voice import script

BASIN = "Upper Assam"
EXTRACTOR = "synthetic-truth"
WORDS_PER_S = 2.5


def inches(label: str) -> float:
    """'9-5/8' -> 9.625, '7' -> 7.0."""
    m = re.fullmatch(r"(\d+)(?:-(\d+)/(\d+))?", label.strip())
    if not m:
        raise ValueError(label)
    return int(m[1]) + (int(m[2]) / int(m[3]) if m[2] else 0.0)


def union(boxes: list[list[float]]) -> list[float]:
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def converted(fs: list[tuple[str, render.Field]]) -> list[dict[str, Any]]:
    """What the canonical layer does to these printed fields (as the pipeline logs it in
    source_ref.conversions): feet to metres; nothing for fields printed in canonical units."""
    return [
        {
            "field": n,
            "printed": re.sub(r"\s*ft$", "", f.printed),  # the number as printed
            "from": "ft",
            "to": "m",
            "factor": 0.3048,
            "value": f.value,
        }
        for n, f in fs
        if f.unit == "ft"
    ]


def wcr_title(w: Well) -> str:
    return f"{w.name} WCR (SYNTHETIC)"


def wcr_fields(w: Well, seed: int) -> tuple[render.Fmt, dict[str, render.Field], int]:
    """The WCR as rendered for this well: printing format, every printed field (value, page,
    bbox) by name, page count. Same random choices as synthetic_assam.cli.render_well."""
    rng = random.Random(seed * 7919 + int(w.name.split("-")[1]) - 1)
    fmt = render.Fmt(w, rng)
    doc = render.wcr(w, fmt, "")
    return fmt, {f.name: f for f in doc.fields}, doc.page


def note_title(w: Well, event_id: str) -> str:
    return f"{w.name} field note {event_id} (SYNTHETIC)"


class Loader:
    def __init__(self, conn: psycopg.Connection, seed: int) -> None:
        self.conn, self.seed = conn, seed
        self.fault_blocks: dict[str, str] = {}
        self.now = datetime.now(UTC)

    def one(self, sql: str, params: tuple[Any, ...]) -> int:
        row = self.conn.execute(sql + " RETURNING id", params).fetchone()
        assert row is not None
        return int(row[0])

    def document(self, well: Well, kind: str, title: str, pages: int, vintage: Any) -> int:
        sha = hashlib.sha256(f"synthetic-truth:{self.seed}:{title}".encode()).digest()
        row = self.conn.execute("SELECT id FROM document WHERE title = %s", (title,)).fetchone()
        if row:
            return int(row[0])
        return self.one(
            "INSERT INTO document (sha256, type, title, vintage, uri, page_count, block_id,"
            " source, synthetic, classification) VALUES (%s, %s, %s, %s, %s, %s, %s,"
            " 'synthetic_assam', true, 'internal')",
            (sha, kind, title, vintage, f"synthetic://{well.name}/{title}", pages, well.block),
        )

    def src(
        self,
        doc: int | None = None,
        page: int | None = None,
        bbox: Any = None,
        audio: int | None = None,
        span: tuple[float, float] | None = None,
        conversions: list[dict[str, Any]] | None = None,
    ) -> int:
        return self.one(
            "INSERT INTO source_ref (document_id, audio_note_id, page, bbox, t_start, t_end,"
            " extractor_version, confidence, review_status, reviewer, reviewed_at, conversions)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 'accepted', %s, %s, %s)",
            (
                doc,
                audio,
                page,
                bbox,
                span[0] if span else None,
                span[1] if span else None,
                EXTRACTOR,
                EXTRACTOR,
                self.now,
                Jsonb(conversions or []),
            ),
        )

    def well(self, w: Well, fid: dict[str, int]) -> None:
        conn = self.conn
        if conn.execute("SELECT 1 FROM well WHERE name = %s", (w.name,)).fetchone():
            return
        fmt, fields, pages = wcr_fields(w, self.seed)
        wcr = self.document(w, "wcr", wcr_title(w), pages, w.completed.date())

        def ref(*names: str) -> int:
            fs = [(n, fields[n]) for n in names if n in fields]
            return self.src(
                wcr, fs[0][1].page, union([f.bbox for _, f in fs]), conversions=converted(fs)
            )

        # The WCR states the coordinate datum, so it is known; the radius is the accuracy of
        # the datum transformation (0 for WGS 84, 22 m for Kalianpur 1975).
        pos = to_wgs84(w.lat_printed_datum, w.lon_printed_datum, w.coord_datum)

        well_id = self.one(
            "INSERT INTO well (name, block_id, field, basin, surface, rt_elevation_m,"
            " ground_elevation_m, spud_date, status, source_ref_id, source_crs,"
            " datum_confidence, position_uncertainty_m, fault_block) VALUES (%s, %s, %s, %s,"
            " ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s, 'completed', %s, %s, %s, %s,"
            " %s)",
            (
                w.name,
                w.block,
                w.field,
                BASIN,
                w.lon,
                w.lat,
                fields["datum_elevation"].value,
                fields["ground_elevation"].value,
                w.spud.date(),
                ref("well_name", "spud_date"),
                pos.source_crs,
                pos.datum_confidence,
                pos.uncertainty_m,
                self.fault_blocks.get(w.name),
            ),
        )
        datum = float(fields["datum_elevation"].value)
        wb = self.one(
            "INSERT INTO wellbore (well_id, name, td_md, status, source_ref_id, datum_type,"
            " datum_elevation_m) VALUES (%s, %s, %s, 'completed', %s, %s, %s)",
            (well_id, w.name, fields["td_md"].value, ref("td_md"), w.datum, datum),
        )
        # Mud weight at the end of each day that has a rendered DDR (day 1 and every event day;
        # the generator also renders one random other day, which is not reproducible here).
        for day in w.days:
            if day.no != 1 and not day.event_ids:
                continue
            ddr = render.ddr(w, day, fmt, "")
            df = {f.name: f for f in ddr.fields}
            doc = self.document(w, "ddr", f"{w.name} DDR {day.no} (SYNTHETIC)", ddr.page, day.date)
            mw_f = df["mud_weight"]
            conn.execute(
                "INSERT INTO mud_record (wellbore_id, ts, md, mw, source_ref_id)"
                " VALUES (%s, %s, %s, %s, %s)",
                (
                    wb,
                    datetime.combine(day.date, datetime.min.time(), UTC) + timedelta(hours=24),
                    day.depth_end,
                    day.mw,
                    self.src(doc, mw_f.page, union([mw_f.bbox, df["depth_md"].bbox])),
                ),
            )
        # Directional survey (the survey report, page 1): trajectories for analogue crossings.
        survey = self.document(w, "survey_report", f"{w.name} SURVEY (SYNTHETIC)", 1,
                               w.completed.date())  # fmt: skip
        s_ref = self.src(survey, 1)
        lat_m = 111_320.0
        lon_m = lat_m * math.cos(math.radians(w.lat))
        for st in w.survey:
            conn.execute(
                "INSERT INTO survey_station (wellbore_id, md, inc, azi, tvd, tvdss, geom,"
                " source_ref_id) VALUES (%s, %s, %s, %s, %s, %s,"
                " ST_SetSRID(ST_MakePoint(%s, %s, %s), 4326), %s)",
                (wb, st.md, st.inc, st.azi % 360, st.tvd, st.tvdss, w.lon + st.east / lon_m,
                 w.lat + st.north / lat_m, -st.tvdss, s_ref),
            )  # fmt: skip
        # The WCR's drilling-problems table documents the whole hole, even when it is empty:
        # an offset without an event in a formation is then "none", not "unknown".
        conn.execute(
            "INSERT INTO event_documentation (wellbore_id, md_top, md_base, kind, source_ref_id)"
            " VALUES (%s, 0, %s, 'wcr_problems', %s)",
            (wb, fields["td_md"].value, self.src(wcr, pages)),
        )
        for i, _ in enumerate(w.tops):
            p = f"tops[{i}]."
            conn.execute(
                "INSERT INTO formation_top (wellbore_id, formation_id, md, tvd, tvdss,"
                " source_ref_id) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    wb,
                    fid[fields[p + "formation"].value],
                    fields[p + "md"].value,
                    fields[p + "tvd"].value,
                    fields[p + "tvdss"].value,
                    ref(p + "formation", p + "md", p + "tvd", p + "tvdss"),
                ),
            )
        casing_ids: dict[str, int] = {}
        for i, c in enumerate(w.casing):
            p = f"casing[{i}]."
            casing_ids[c.od_in] = self.one(
                "INSERT INTO casing_string (wellbore_id, kind, od_in, hole_size_in, weight_ppf,"
                " grade, md_top, md_shoe, tvdss_shoe, source_ref_id) VALUES (%s, %s, %s, %s,"
                " %s, %s, 0, %s, %s, %s)",
                (
                    wb,
                    c.kind,
                    inches(c.od_in),
                    inches(c.hole_in),
                    c.weight_ppf,
                    c.grade,
                    fields[p + "shoe_md"].value,
                    float(fields[p + "shoe_tvd"].value) - datum,
                    ref(
                        p + "od_in",
                        p + "hole_in",
                        p + "shoe_md",
                        p + "shoe_tvd",
                        p + "weight_ppf",
                        p + "grade",
                    ),
                ),
            )
        for i, lf in enumerate(w.lot_fit):
            p = f"lot_fit[{i}]."
            conn.execute(
                "INSERT INTO lot_fit (wellbore_id, casing_string_id, test_type, md, emw,"
                " source_ref_id) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    wb,
                    casing_ids.get(lf.casing),
                    lf.test_type,
                    fields[p + "md"].value,
                    lf.emw,
                    ref(p + "test_type", p + "casing", p + "md", p + "emw"),
                ),
            )
        for i, e in enumerate(w.events):
            p = f"events[{i}]."
            assert e.t_start is not None and e.t_end is not None
            ev_src = ref(
                *(p + k for k in ("date", "type", "md", "formation", "npt_hr", "volume_bbl", "mw"))
            )
            narrative = render.narrative(e, fmt)
            cause = e.root_cause if e.root_cause_status != "unknown" else None
            ev = self.one(
                "INSERT INTO event (wellbore_id, type, md_top, tvdss_top, formation_id, rel_pos,"
                " t_start, t_end, npt_hr, severity, loss_bbl, mw, ecd, narrative, root_cause,"
                " root_cause_status, outcome, source_ref_id) VALUES (%s, %s, %s, %s, %s, %s,"
                " %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    wb,
                    e.type,
                    fields[p + "md"].value,
                    e.tvdss,
                    fid[e.formation],
                    e.rel_pos,
                    e.t_start,
                    e.t_end,
                    e.npt_hr,
                    e.severity,
                    e.loss_bbl,
                    e.mw,
                    e.ecd,
                    narrative,
                    cause,
                    e.root_cause_status if cause else "unknown",
                    e.outcome,
                    ev_src,
                ),
            )
            for j, action in enumerate(e.mitigations):
                conn.execute(
                    "INSERT INTO mitigation (event_id, action, t_applied, source_ref_id)"
                    " VALUES (%s, %s, %s, %s)",
                    (ev, action, e.t_start + timedelta(hours=j + 1), ev_src),
                )
            # Retrieval passages: the field note (handwritten, page 1) and the voice note.
            # Field notes register as doc_type 'other' (synthetic_assam.cli.DOC_TYPE_DB).
            note = self.document(w, "other", note_title(w, e.id), 1, e.t_start.date())
            conn.execute(
                "INSERT INTO chunk (source_ref_id, text) VALUES (%s, %s)",
                (self.src(note, 1), f"{w.name} field note, {e.t_start.date()}: {narrative}"),
            )
            spoken = script(w, e)
            words = len(spoken.split())
            audio = self.one(
                "INSERT INTO audio_note (sha256, uri, duration_s, speaker, lang, recorded_at,"
                " block_id, source, synthetic) VALUES (%s, %s, %s, 'synthetic voice', 'en', %s,"
                " %s, 'synthetic_assam', true)",
                (
                    hashlib.sha256(f"{self.seed}:{e.id}:voice".encode()).digest(),
                    f"synthetic://{w.name}/voice/{e.id}.wav",
                    words / WORDS_PER_S,
                    e.t_end,
                    w.block,
                ),
            )
            t = 0.0
            for s in sentences(spoken):
                dur = len(s.split()) / WORDS_PER_S
                if not s.startswith(("This is a synthetic", "Note on well")):
                    conn.execute(
                        "INSERT INTO chunk (source_ref_id, text) VALUES (%s, %s)",
                        (
                            self.src(audio=audio, span=(round(t, 1), round(t + dur, 1))),
                            f"{w.name} voice note on the {e.type.replace('_', ' ')}: {s}",
                        ),
                    )
                t += dur


FAULT_STRIKE_DEG = 45.0  # NE-SW, parallel to the Naga thrust front


def fault_blocks(wells: list[Well]) -> dict[str, str]:
    """SYNTHETIC structural interpretation: each field is cut by one NE-SW fault through its
    centroid; a well's fault block is the field plus the side of that fault it sits on. A
    placeholder for a geologist's interpretation, so the analogue fault-block factor has data."""
    out: dict[str, str] = {}
    by_field: dict[str, list[Well]] = {}
    for w in wells:
        by_field.setdefault(w.field, []).append(w)
    s = math.radians(FAULT_STRIKE_DEG)
    for field, ws in by_field.items():
        lat0 = sum(w.lat for w in ws) / len(ws)
        lon0 = sum(w.lon for w in ws) / len(ws)
        kx = math.cos(math.radians(lat0))
        for w in ws:
            x, y = (w.lon - lon0) * kx, w.lat - lat0
            side = "NW" if math.sin(s) * y - math.cos(s) * x > 0 else "SE"
            out[w.name] = f"{field.split()[-1]}-{side}"
    return out


def load(conn: psycopg.Connection, wells: int = 30, seed: int = 1) -> list[Well]:
    cfg = load_config()
    built = [build_well(i, seed, cfg) for i in range(wells)]
    ld = Loader(conn, seed)
    ld.fault_blocks = fault_blocks(built)
    for b in sorted({w.block for w in built}):
        conn.execute(
            "INSERT INTO block (id, name, basin) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (b, f"Synthetic block {b}", BASIN),
        )
    conn.execute(
        "INSERT INTO basin (id, name) VALUES ('Assam-Arakan', 'Assam-Arakan Basin'),"
        " (%s, 'Upper Assam Shelf') ON CONFLICT DO NOTHING",
        (BASIN,),
    )
    for f in cfg["formation"]:
        conn.execute(
            "INSERT INTO formation (basin, name, synonyms) VALUES (%s, %s, %s)"
            " ON CONFLICT (basin, name) DO NOTHING",
            (BASIN, f["name"], f.get("synonyms", [])),
        )
    fid: dict[str, int] = dict(
        conn.execute("SELECT name, id FROM formation WHERE basin = %s", (BASIN,)).fetchall()
    )
    for w in built:
        ld.well(w, fid)
    return built


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--wells", type=int, default=30)
    p.add_argument("--seed", type=int, default=1)
    a = p.parse_args()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        built = load(conn, a.wells, a.seed)
    print(f"loaded {len(built)} synthetic wells (accepted facts, SYNTHETIC)")


if __name__ == "__main__":
    main()
