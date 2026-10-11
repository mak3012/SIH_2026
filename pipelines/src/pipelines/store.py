"""Write pipeline output to Postgres: accepted facts with a source_ref (page + bbox), everything
else to review_item. Runs as the table owner (trusted ingestion; bypasses RLS).

A table row (a formation top, a casing string, ...) is stored only when every field it needs is
accepted and it can be attached (known well, known formation). Otherwise the row goes to review
as one item, alongside the per-field items that explain why. Rerunning a document with the same
extractor version stores nothing twice.
"""

import re
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from core.canonical.crs import offset_to_lonlat, to_wgs84
from core.canonical.ontology import Decision, Ontology, Unit
from pipelines.ddr import StitchedEvent
from pipelines.extract import PROBLEMS_SECTION
from pipelines.norm import size_inches
from pipelines.process import DocResult, FieldResult

REQUIRED_ROW = {
    "tops": ["formation", "md"],
    "casing": ["od_in", "shoe_md"],
    "lot_fit": ["test_type", "md", "emw"],
    "events": ["type", "md"],
    "survey": ["md", "inc", "azi", "tvd"],
}


class Store:
    def __init__(
        self,
        conn: psycopg.Connection,
        document: dict[str, Any],
        result: DocResult,
        threshold: float,
    ) -> None:
        self.conn, self.doc, self.r, self.threshold = conn, document, result, threshold
        self.counts = {"facts": 0, "review": 0}
        self._ontology: Ontology | None = None

    # ------------------------------------------------------------------ plumbing

    def source_ref(self, fields: Iterable[FieldResult]) -> int:
        fs = list(fields)
        box = [
            min(f.bbox[0] for f in fs),
            min(f.bbox[1] for f in fs),
            max(f.bbox[2] for f in fs),
            max(f.bbox[3] for f in fs),
        ]
        conversions = [{"field": f.name, **c} for f in fs for c in f.conversions]
        row = self.conn.execute(
            "INSERT INTO source_ref (document_id, page, bbox, extractor_version, confidence,"
            " conversions) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (
                self.doc["id"],
                fs[0].page,
                box,
                self.r.extractor_version,
                min(f.confidence for f in fs),
                Jsonb(conversions),
            ),
        ).fetchone()
        assert row is not None
        return int(row[0])

    def review(
        self,
        kind: str,
        field: str,
        value: Any,
        reason: str,
        *,
        f: FieldResult | None = None,
        failures: list[str] | None = None,
    ) -> None:
        cur = self.conn.execute(
            "INSERT INTO review_item (document_id, kind, doc_type, field, value, printed, page,"
            " bbox, confidence, components, rule_failures, reason, extractor_version)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT DO NOTHING",
            (
                self.doc["id"],
                kind,
                self.r.doc_type,
                field,
                Jsonb(value),
                f.printed if f else None,
                f.page if f else None,
                f.bbox if f else None,
                f.confidence if f else 0.0,
                Jsonb(f.components if f else {}),
                Jsonb(failures if failures is not None else (f.failures if f else [])),
                reason,
                self.r.extractor_version,
            ),
        )
        self.counts["review"] += cur.rowcount

    def ok(self, name: str) -> FieldResult | None:
        f = self.r.fields.get(name)
        return f if f is not None and f.accepted else None

    def val(self, name: str) -> Any:
        f = self.ok(name)
        return None if f is None else f.value

    def rows(self, prefix: str) -> list[dict[str, FieldResult]]:
        out: dict[int, dict[str, FieldResult]] = {}
        for n, f in self.r.fields.items():
            m = re.match(rf"^{prefix}\[(\d+)\]\.(\w+)$", n)
            if m:
                out.setdefault(int(m.group(1)), {})[m.group(2)] = f
        return [out[i] for i in sorted(out)]

    def row_ready(self, prefix: str, i: int, row: dict[str, FieldResult]) -> bool:
        missing = [k for k in REQUIRED_ROW[prefix] if k not in row or not row[k].accepted]
        if missing:
            self.review(
                "row",
                f"{prefix}[{i}]",
                {k: f.value for k, f in row.items()},
                f"row not stored: {', '.join(missing)} missing or below threshold",
            )
        return not missing

    # ------------------------------------------------------------------ lookups

    def well(self, create: bool) -> tuple[int, float, float, float] | None:
        """(wellbore id, datum elevation, lon, lat) of the document's well, creating it from a
        WCR. The datum is the wellbore's own depth reference, falling back to the well's."""
        name = self.val("well_name")
        if not name:
            self.review(
                "document",
                "well_name",
                None,
                "well name missing or not accepted",
                f=self.r.fields.get("well_name"),
            )
            return None
        row = self.conn.execute(
            "SELECT b.id, coalesce(b.datum_elevation_m, w.rt_elevation_m), ST_X(w.surface),"
            " ST_Y(w.surface) FROM well w"
            " JOIN wellbore b ON b.well_id = w.id AND b.parent_id IS NULL WHERE w.name = %s",
            (name,),
        ).fetchone()
        if row:
            return int(row[0]), float(row[1]), float(row[2]), float(row[3])
        need = ["latitude", "longitude", "coordinate_datum", "datum_elevation"]
        if not create or any(self.ok(n) is None for n in need):
            self.review(
                "document",
                "well",
                {"well_name": name},
                f"well {name} unknown"
                + ("" if create else "; process its WCR first")
                + (" and its location/elevation are not all accepted" if create else ""),
            )
            return None
        # The report states its datum (R-DATUM refuses coordinates without one), so the
        # confidence is 'known' and the radius is the pinned transformation's accuracy.
        pos = to_wgs84(
            self.val("latitude"), self.val("longitude"), self.val("coordinate_datum"), "known"
        )
        lat, lon = pos.lat, pos.lon
        rt = self.val("datum_elevation")
        ref = self.val("depth_reference")
        src = self.source_ref([self.r.fields[n] for n in ["well_name", *need]])
        wid = self.conn.execute(
            "INSERT INTO well (name, block_id, jv_id, field, surface, rt_elevation_m,"
            " ground_elevation_m, spud_date, source_ref_id, source_crs, datum_confidence,"
            " position_uncertainty_m) VALUES (%s, %s, %s, %s,"
            " ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                name,
                self.doc["block_id"],
                self.doc.get("jv_id"),
                self.val("field"),
                lon,
                lat,
                rt,
                self.val("ground_elevation"),
                self.val("spud_date"),
                src,
                pos.source_crs,
                pos.datum_confidence,
                pos.uncertainty_m,
            ),
        ).fetchone()[0]  # type: ignore[index]
        bid = self.conn.execute(
            "INSERT INTO wellbore (well_id, name, td_md, source_ref_id, datum_type,"
            " datum_elevation_m) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (wid, name, self.val("td_md"), src, ref, rt if ref else None),
        ).fetchone()[0]  # type: ignore[index]
        self.counts["facts"] += 2
        return int(bid), float(rt), float(lon), float(lat)

    def formation_id(self, name: str, tvdss: float | None = None) -> int | None:
        """Formation id for a printed name, if the ontology rules confirm one. Every decision
        (confirmed, needs a human, rejected) is recorded in name_match."""
        if self._ontology is None:
            self._ontology = load_ontology(self.conn)
        d = self._ontology.resolve(name, tvdss=tvdss)
        record_match(self.conn, "formation", name, d, document_id=self.doc["id"])
        if d.status == "confirmed" and d.unit_id is not None:
            return int(d.unit_id)
        return None

    # ------------------------------------------------------------------ per document type

    def run(self) -> dict[str, int]:
        done = self.conn.execute(
            "SELECT 1 FROM source_ref WHERE document_id = %s AND extractor_version = %s"
            " UNION ALL SELECT 1 FROM review_item WHERE document_id = %s AND extractor_version = %s"
            " LIMIT 1",
            (self.doc["id"], self.r.extractor_version) * 2,
        ).fetchone()
        if done:
            return {"facts": 0, "review": 0, "skipped": 1}
        for f in self.r.review():
            self.review("field", f.name, f.value, _why(f, self.threshold), f=f)
        for fail in self.r.failures:
            if not fail.fields or any(n not in self.r.fields for n in fail.fields):
                self.review("document", fail.rule, None, fail.message, failures=[fail.message])
        getattr(self, f"_{self.r.doc_type}", lambda: None)()
        return self.counts

    def _wcr(self) -> None:
        w = self.well(create=True)
        if w is None:
            return
        bore, rt, _, _ = w
        if PROBLEMS_SECTION in self.r.notes and self.ok("td_md"):
            self.documented(bore, 0.0, self.val("td_md"), "wcr_problems", ["well_name", "td_md"])
        for i, row in enumerate(self.rows("tops")):
            if not self.row_ready("tops", i, row):
                continue
            tvd_ok = row["tvd"].value if "tvd" in row and row["tvd"].accepted else None
            fid = self.formation_id(
                row["formation"].value, tvd_ok - rt if tvd_ok is not None else None
            )
            if fid is None:
                self.review(
                    "row",
                    f"tops[{i}]",
                    {"formation": row["formation"].value},
                    f"unknown formation {row['formation'].value!r}",
                    f=row["formation"],
                )
                continue
            ok = [f for f in row.values() if f.accepted]
            tvd = row["tvd"].value if "tvd" in row and row["tvd"].accepted else None
            self._insert(
                "formation_top",
                {
                    "wellbore_id": bore,
                    "formation_id": fid,
                    "md": row["md"].value,
                    "tvd": tvd,
                    "tvdss": tvd - rt if tvd is not None else None,
                },
                ok,
            )
        casing_ids = {}
        for i, row in enumerate(self.rows("casing")):
            if not self.row_ready("casing", i, row):
                continue
            od = row["od_in"].value
            tvd = row["shoe_tvd"].value if "shoe_tvd" in row and row["shoe_tvd"].accepted else None
            casing_ids[od] = self._insert(
                "casing_string",
                {
                    "wellbore_id": bore,
                    "kind": _casing_kind(od),
                    "od_in": size_inches(od),
                    "hole_size_in": _opt(row, "hole_in", size_inches),
                    "weight_ppf": _opt(row, "weight_ppf"),
                    "grade": _opt(row, "grade"),
                    "md_shoe": row["shoe_md"].value,
                    "tvdss_shoe": tvd - rt if tvd is not None else None,
                },
                [f for f in row.values() if f.accepted],
            )
        for i, row in enumerate(self.rows("lot_fit")):
            if self.row_ready("lot_fit", i, row):
                self._insert(
                    "lot_fit",
                    {
                        "wellbore_id": bore,
                        "casing_string_id": casing_ids.get(_opt(row, "casing")),
                        "test_type": row["test_type"].value,
                        "md": row["md"].value,
                        "emw": row["emw"].value,
                    },
                    [f for f in row.values() if f.accepted],
                )
        for i, row in enumerate(self.rows("events")):
            if not self.row_ready("events", i, row):
                continue
            kind = row["type"].value
            form = _opt(row, "formation")
            day = _opt(row, "date")
            self._insert(
                "event",
                {
                    "wellbore_id": bore,
                    "type": kind,
                    "md_top": row["md"].value,
                    "formation_id": self.formation_id(form) if form else None,
                    "t_start": _ts(day),
                    "npt_hr": _opt(row, "npt_hr"),
                    "loss_bbl": _opt(row, "volume_bbl") if kind == "loss" else None,
                    "mw": _opt(row, "mw"),
                    "narrative": None,
                },
                [f for f in row.values() if f.accepted],
            )

    def _survey_report(self) -> None:
        w = self.well(create=False)
        if w is None:
            return
        bore, rt, lon0, lat0 = w
        for i, row in enumerate(self.rows("survey")):
            if not self.row_ready("survey", i, row):
                continue
            north, east = _opt(row, "north") or 0.0, _opt(row, "east") or 0.0
            lon, lat = offset_to_lonlat(lon0, lat0, north, east)
            tvd = row["tvd"].value
            ok = [f for f in row.values() if f.accepted]
            src = self.source_ref(ok)
            self.conn.execute(
                "INSERT INTO survey_station (wellbore_id, md, inc, azi, tvd, tvdss, geom,"
                " source_ref_id) VALUES (%s, %s, %s, %s, %s, %s,"
                " ST_SetSRID(ST_MakePoint(%s, %s, %s), 4326), %s)",
                (
                    bore,
                    row["md"].value,
                    row["inc"].value,
                    row["azi"].value % 360,
                    tvd,
                    tvd - rt,
                    lon,
                    lat,
                    -(tvd - rt),
                    src,
                ),
            )
            self.counts["facts"] += 1

    def _ddr(self) -> None:
        w = self.well(create=False)
        if w is None:
            return
        bore = w[0]
        if self.ok("depth_md") and self.ok("progress"):
            depth, progress = self.val("depth_md"), self.val("progress")
            self.documented(
                bore,
                max(0.0, depth - progress),
                depth,
                "ddr",
                ["report_date", "depth_md", "progress"],
            )
        need = ["report_date", "mud_weight", "depth_md"]
        if all(self.ok(n) for n in need):
            self._insert(
                "mud_record",
                {
                    "wellbore_id": bore,
                    "ts": _ts(self.val("report_date")),
                    "md": self.val("depth_md"),
                    "mw": self.val("mud_weight"),
                },
                [self.r.fields[n] for n in need],
            )
        # Events come from stitching all of a well's DDRs together: see store_ddr_events.

    def _field_note(self) -> None:
        w = self.well(create=False)
        if w is None or not all(self.ok(n) for n in ("event_type", "depth_md")):
            return
        ok = [f for f in self.r.fields.values() if f.accepted]
        kind = self.val("event_type")
        form = self.val("formation")
        ev = self._insert(
            "event",
            {
                "wellbore_id": w[0],
                "type": kind,
                "md_top": self.val("depth_md"),
                "formation_id": self.formation_id(form) if form else None,
                "t_start": _ts(self.val("date")),
                "npt_hr": self.val("npt_hours"),
                "loss_bbl": self.val("loss_bbl") if kind == "loss" else None,
                "mw": self.val("mud_weight"),
                "narrative": " / ".join(f.printed for f in self.r.fields.values()),
            },
            ok,
        )
        src = self.conn.execute("SELECT source_ref_id FROM event WHERE id = %s", (ev,)).fetchone()
        assert src is not None
        for action in self.val("mitigation") or []:
            self.conn.execute(
                "INSERT INTO mitigation (event_id, action, source_ref_id) VALUES (%s, %s, %s)",
                (ev, action, src[0]),
            )
            self.counts["facts"] += 1

    def documented(self, bore: int, top: float, base: float, kind: str, names: list[str]) -> None:
        """This interval's drilling problems were documented (see event_documentation)."""
        fields = [self.r.fields[n] for n in names if n in self.r.fields]
        self._insert(
            "event_documentation",
            {"wellbore_id": bore, "md_top": top, "md_base": base, "kind": kind},
            fields,
        )

    def _insert(self, table: str, cols: dict[str, Any], fields: list[FieldResult]) -> int:
        cols = {**cols, "source_ref_id": self.source_ref(fields)}
        names = ", ".join(cols)
        marks = ", ".join(["%s"] * len(cols))
        row = self.conn.execute(
            f"INSERT INTO {table} ({names}) VALUES ({marks}) RETURNING id", list(cols.values())
        ).fetchone()
        assert row is not None
        self.counts["facts"] += 1
        return int(row[0])


def _why(f: FieldResult, threshold: float) -> str:
    low = [k for k, v in f.components.items() if v < threshold]
    return f"confidence {f.confidence:.2f} < {threshold} ({', '.join(low) or 'unreadable'})"


def _opt(row: dict[str, FieldResult], key: str, fn: Any = None) -> Any:
    f = row.get(key)
    if f is None or not f.accepted or f.value is None:
        return None
    return fn(f.value) if fn else f.value


def _ts(day: str | None) -> datetime | None:
    return datetime.combine(date.fromisoformat(day), datetime.min.time(), UTC) if day else None


def _casing_kind(od: str) -> str:
    size = size_inches(od)
    return "surface" if size >= 13 else "intermediate" if size >= 8.5 else "production"


def store_document(
    conn: psycopg.Connection, document: dict[str, Any], result: DocResult, threshold: float
) -> dict[str, int]:
    with conn.transaction():
        return Store(conn, document, result, threshold).run()


def store_ddr_events(
    conn: psycopg.Connection, well_name: str, events: list[StitchedEvent], version: str
) -> int:
    """Stitched DDR events as `event` rows with a timeline; provenance = the first row, with the
    lowest confidence of all its rows."""
    n = 0
    with conn.transaction():
        row = conn.execute(
            "SELECT b.id FROM well w JOIN wellbore b ON b.well_id = w.id"
            " AND b.parent_id IS NULL WHERE w.name = %s",
            (well_name,),
        ).fetchone()
        if row is None:
            return 0
        for e in events:
            first = e.timeline[0]
            if first["document_id"] is None:
                continue
            exists = conn.execute(
                "SELECT 1 FROM event ev JOIN source_ref r ON r.id = ev.source_ref_id"
                " WHERE ev.wellbore_id = %s AND ev.t_start = %s AND ev.type = %s"
                " AND r.extractor_version = %s",
                (row[0], e.t_start.replace(tzinfo=UTC), e.type, version),
            ).fetchone()
            if exists:
                continue
            src = conn.execute(
                "INSERT INTO source_ref (document_id, page, bbox, extractor_version, confidence)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (
                    first["document_id"],
                    first["page"],
                    first["bbox"],
                    version,
                    min(t["confidence"] for t in e.timeline),
                ),
            ).fetchone()
            assert src is not None
            conn.execute(
                "INSERT INTO event (wellbore_id, type, md_top, t_start, t_end, npt_hr, narrative,"
                " timeline, source_ref_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    row[0],
                    e.type,
                    e.depth_m,
                    e.t_start.replace(tzinfo=UTC),
                    e.t_end.replace(tzinfo=UTC),
                    e.npt_hr,
                    " / ".join(t["description"] for t in e.timeline),
                    Jsonb(e.timeline),
                    src[0],
                ),
            )
            n += 1
    return n


def load_ontology(conn: psycopg.Connection) -> Ontology:
    """The formation ontology from the basin and formation tables (ids as strings)."""
    units: list[Unit] = []
    for bid, name, parent in conn.execute("SELECT id, name, parent_id FROM basin ORDER BY id"):
        units.append(
            Unit(
                f"basin:{bid}",
                name,
                "sub_basin" if parent else "basin",
                f"basin:{parent}" if parent else None,
            )
        )
    rows = conn.execute(
        "SELECT id, basin, name, parent_id, synonyms, abbreviations, rank, expected_tvdss_top,"
        " expected_tvdss_base FROM formation ORDER BY parent_id NULLS FIRST, id"
    ).fetchall()
    for fid, basin, name, parent, syns, abbr, rank, lo, hi in rows:
        units.append(
            Unit(
                str(fid),
                name,
                rank,
                str(parent) if parent else f"basin:{basin}",
                list(syns or []),
                list(abbr or []),
                (lo, hi) if lo is not None and hi is not None else None,
            )
        )
    return Ontology(units)


def record_match(
    conn: psycopg.Connection,
    kind: str,
    printed: str,
    d: Decision,
    *,
    document_id: int | None = None,
    audio_note_id: int | None = None,
) -> None:
    """One name-resolution decision, whoever proposed it."""
    proposed = (
        "llm"
        if d.checks.get("suggested_by") == "llm"
        else ("human" if d.checks.get("human") else "rule")
    )
    unit = int(d.unit_id) if d.unit_id is not None and d.unit_id.isdigit() else None
    conn.execute(
        "INSERT INTO name_match (kind, printed, formation_id, well_id, proposed_by, status,"
        " confirmed_by, checks, reason, document_id, audio_note_id)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            kind,
            printed,
            unit if kind == "formation" else None,
            unit if kind == "well" else None,
            proposed,
            d.status,
            d.confirmed_by if d.status == "confirmed" else None,
            Jsonb(d.checks),
            d.reason,
            document_id,
            audio_note_id,
        ),
    )
