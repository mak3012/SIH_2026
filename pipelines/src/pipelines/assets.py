"""Dagster assets. One dynamic partition per registered document / audio note.

documents:   document_file -> document_type -> page_images -> page_layout -> extraction_passes
             -> validated_extraction -> stored_facts        (partitioned by document id)
             ddr_events                                      (all DDRs, stitched per well)
voice notes: audio_file -> transcript -> lesson_candidates -> stored_lessons (by audio note id)

The sensors add a partition for every registered document / audio note and request a run.
"""

import functools
import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psycopg
from dagster import (
    AssetExecutionContext,
    AssetSelection,
    ConfigurableResource,
    DefaultScheduleStatus,
    DynamicPartitionsDefinition,
    MaterializeResult,
    RunRequest,
    ScheduleDefinition,
    SensorEvaluationContext,
    SensorResult,
    asset,
    define_asset_job,
    sensor,
)
from opentelemetry import trace

from core import telemetry
from pipelines import process as proc
from pipelines import sodir
from pipelines.classify import classify
from pipelines.ddr import Row, depth_in, stitch
from pipelines.extract import PARSERS
from pipelines.layout import PageLayout, render_pages, text_layer
from pipelines.llm import LLM, JsonlLog, LLMConfig, PgLog
from pipelines.reanchor import reanchor_wellbore, stale_wellbores
from pipelines.store import store_ddr_events, store_document
from pipelines.voice import (
    ASR_VERSION,
    Lesson,
    Transcript,
    extract_lessons,
    formation_names,
    store_lessons,
    transcribe,
)

documents = DynamicPartitionsDefinition(name="document")
telemetry.setup("pipelines")


def traced[F: Callable[..., Any]](fn: F) -> F:
    """One span per asset step. Steps run in separate processes, so the trace id is derived
    from (run id, partition): every step of one document's run lands in the same trace."""

    @functools.wraps(fn)
    def wrapper(context: AssetExecutionContext, *a: Any, **k: Any) -> Any:
        key = f"{context.run.run_id}/{context.partition_key}"
        h = hashlib.sha256(key.encode()).digest()
        parent = trace.NonRecordingSpan(
            trace.SpanContext(
                int.from_bytes(h[:16]),
                int.from_bytes(h[16:24]),
                is_remote=True,
                trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED),
            )
        )
        attrs = {"dagster.run_id": context.run.run_id, "partition": context.partition_key}
        with telemetry.tracer.start_as_current_span(
            f"ingest.{fn.__name__}", trace.set_span_in_context(parent), attributes=attrs
        ):
            return fn(context, *a, **k)

    return wrapper  # type: ignore[return-value]


audio_notes = DynamicPartitionsDefinition(name="audio_note")


class Database(ConfigurableResource):  # type: ignore[type-arg]
    url: str

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.url)


class Storage(ConfigurableResource):  # type: ignore[type-arg]
    """Object store (S3) plus the local work directory for pages and results."""

    endpoint: str = ""
    access_key: str = ""
    secret_key: str = ""
    work_dir: str = "/opt/dagster/work"
    local_root: str = ""  # when set, s3://bucket/key is read from <local_root>/<key> instead

    def fetch(self, uri: str) -> Path:
        u = urlsplit(uri)
        key = u.path.lstrip("/")
        if self.local_root:
            return Path(self.local_root) / key
        dest = Path(self.work_dir) / "objects" / u.netloc / key
        if not dest.exists():
            from minio import Minio

            e = urlsplit(self.endpoint)
            Minio(
                e.netloc, self.access_key, self.secret_key, secure=e.scheme == "https"
            ).fget_object(u.netloc, key, str(dest))
        return dest

    def dir(self, *parts: str) -> Path:
        p = Path(self.work_dir, *parts)
        p.mkdir(parents=True, exist_ok=True)
        return p


class Models(ConfigurableResource):  # type: ignore[type-arg]
    llm_backend: str = "none"
    llm_url: str = "http://ollama:11434"
    llm_model: str = "qwen2.5:7b-instruct"
    vlm_model: str = "qwen2.5vl:7b"
    whisper_model: str = "small"
    confidence_threshold: float = 0.8
    second_pass: str = "auto"
    layout_engines: str = "text_layer,docling,paddleocr"

    def settings(self) -> proc.Settings:
        return proc.Settings(
            threshold=self.confidence_threshold,
            second_pass=self.second_pass,
            engines=tuple(self.layout_engines.split(",")),
        )

    def llm(self, db: Database | None, work: Path) -> LLM:
        cfg = LLMConfig(self.llm_backend, self.llm_url, self.llm_model, self.vlm_model)
        log = PgLog(db.connect) if db is not None and db.url else JsonlLog(work / "llm_calls.jsonl")
        return LLM(cfg, log)


def _row(db: Database, table: str, key: str) -> dict[str, Any]:
    with db.connect() as conn:
        cur = conn.execute(f"SELECT * FROM {table} WHERE id = %s", (int(key),))
        names = [d.name for d in cur.description or []]
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"{table} {key} not found")
    return dict(zip(names, row, strict=True))


# ---------------------------------------------------------------- documents


@asset(partitions_def=documents, group_name="documents")
@traced
def document_file(context: AssetExecutionContext, db: Database, storage: Storage) -> dict[str, Any]:
    """The registered document, fetched from object storage."""
    doc = _row(db, "document", context.partition_key)
    path = storage.fetch(doc["uri"])
    context.add_output_metadata({"path": str(path), "type": doc["type"], "title": doc["title"]})
    return {k: doc[k] for k in ("id", "type", "title", "block_id", "jv_id", "synthetic")} | {
        "path": str(path)
    }


@asset(partitions_def=documents, group_name="documents")
@traced
def document_type(context: AssetExecutionContext, document_file: dict[str, Any]) -> dict[str, Any]:
    """First guess from the text layer, file name and registered type (re-checked after OCR)."""
    pages = text_layer(document_file["path"])[:2]
    kind, conf, scores = classify(
        proc.page_text(pages), Path(document_file["path"]).name, document_file["type"]
    )
    context.add_output_metadata({"doc_type": kind, "confidence": conf})
    return {"doc_type": kind, "confidence": conf, "scores": scores}


@asset(partitions_def=documents, group_name="documents")
@traced
def page_images(
    context: AssetExecutionContext, document_file: dict[str, Any], storage: Storage, models: Models
) -> list[str]:
    """Every page rendered at 300 dpi (grayscale PNG)."""
    out = storage.dir("pages", context.partition_key)
    paths = render_pages(document_file["path"], out, models.settings().dpi)
    context.add_output_metadata({"pages": len(paths)})
    return [str(p) for p in paths]


@asset(partitions_def=documents, group_name="documents")
@traced
def page_layout(
    context: AssetExecutionContext,
    document_file: dict[str, Any],
    document_type: dict[str, Any],
    page_images: list[str],
    storage: Storage,
    models: Models,
    db: Database,
) -> list[PageLayout]:
    """Cells per page: text layer, else Docling, else PaddleOCR; VLM for hard pages."""
    llm = models.llm(db, storage.dir())
    pages, notes = proc.layout(
        Path(document_file["path"]), models.settings(), llm,
        Path(page_images[0]).parent if page_images else storage.dir("pages", context.partition_key),
        document_type["doc_type"], document_id=document_file["id"],
    )  # fmt: skip
    context.add_output_metadata(
        {
            "engines": sorted({p.engine for p in pages}),
            "notes": notes,
            "cells": sum(len(p.cells) for p in pages),
        }
    )
    return pages


@asset(partitions_def=documents, group_name="documents")
@traced
def extraction_passes(
    context: AssetExecutionContext,
    document_file: dict[str, Any],
    document_type: dict[str, Any],
    page_layout: list[PageLayout],
    storage: Storage,
    models: Models,
    db: Database,
) -> dict[str, Any]:
    """Schema-guided extraction, twice: template parser and (if configured) LLM or OCR pass."""
    kind, conf, _ = classify(
        proc.page_text(page_layout[:2]), Path(document_file["path"]).name, document_file["type"]
    )
    if document_type["confidence"] > conf:
        kind, conf = document_type["doc_type"], document_type["confidence"]
    if kind not in PARSERS:
        return {"doc_type": kind, "class_conf": conf, "a": None, "b": None, "engines": []}
    a, b, pages = proc.passes(kind, page_layout, Path(document_file["path"]), models.settings(),
                       models.llm(db, storage.dir()), storage.dir("pages", context.partition_key),
                       document_id=document_file["id"])  # fmt: skip
    context.add_output_metadata(
        {"doc_type": kind, "pass_a": len(a.values), "pass_b": len(b.values) if b else 0}
    )
    return {
        "doc_type": kind,
        "class_conf": conf,
        "a": a,
        "b": b,
        "engines": sorted({p.engine for p in pages}),
    }


@asset(partitions_def=documents, group_name="documents")
@traced
def validated_extraction(
    context: AssetExecutionContext,
    extraction_passes: dict[str, Any],
    page_layout: list[PageLayout],
    storage: Storage,
    models: Models,
) -> proc.DocResult:
    """Rules, confidence = min(ocr, consistency, rules), accept or review."""
    x = extraction_passes
    engines = x["engines"] or sorted({p.engine for p in page_layout})
    if x["a"] is None:
        r = proc.unsupported(x["doc_type"], x["class_conf"], engines, [])
    else:
        r = proc.decide(x["doc_type"], x["class_conf"], x["a"], x["b"], engines, models.settings())
    (storage.dir("results") / f"{context.partition_key}.json").write_text(
        json.dumps(r.to_json(), default=str)
    )
    context.add_output_metadata(
        {
            "fields": len(r.fields),
            "accepted": len(r.accepted()),
            "review": len(r.review()),
            "failures": [f.message for f in r.failures][:20],
        }
    )
    return r


@asset(partitions_def=documents, group_name="documents")
@traced
def stored_facts(
    context: AssetExecutionContext,
    document_file: dict[str, Any],
    validated_extraction: proc.DocResult,
    db: Database,
    models: Models,
) -> MaterializeResult[Any]:
    """Accepted facts with source_ref (page + bbox) into Postgres; the rest into review_item."""
    with db.connect() as conn:
        counts = store_document(
            conn, document_file, validated_extraction, models.confidence_threshold
        )
    return MaterializeResult(metadata=counts)


@asset(group_name="documents", deps=[stored_facts])
def ddr_events(
    context: AssetExecutionContext, db: Database, storage: Storage
) -> MaterializeResult[Any]:
    """Stitch every processed DDR, per well, into events with a timeline (accepted rows only)."""
    by_well: dict[str, list[Row]] = {}
    version = None
    for path in sorted(storage.dir("results").glob("*.json")):
        r = json.loads(path.read_text())
        if r["doc_type"] != "ddr":
            continue
        f = r["fields"]
        well = f.get("well_name", {})
        day = f.get("report_date", {})
        if not (well.get("accepted") and day.get("accepted")):
            continue
        version = r["extractor_version"]
        for i in range(500):
            cells = {k: f.get(f"time_breakdown[{i}].{k}") for k in
                     ("from", "to", "hours", "description")}  # fmt: skip
            if cells["from"] is None:
                break
            if not all(c and c["accepted"] for c in cells.values()):
                continue  # already in the review queue
            desc = str(cells["description"]["value"])
            by_well.setdefault(well["value"], []).append(Row(
                _date(day["value"]), cells["from"]["value"], cells["to"]["value"],
                cells["hours"]["value"], desc, depth_in(desc, r["depth_unit"]),
                int(path.stem), cells["description"]["page"], cells["description"]["bbox"],
                min(c["confidence"] for c in cells.values()),
            ))  # fmt: skip
    n = 0
    with db.connect() as conn:
        for well, rows in by_well.items():
            n += store_ddr_events(conn, well, stitch(rows), f"stitch@1.0.0+{version}")
    return MaterializeResult(metadata={"wells": len(by_well), "events_stored": n})


def _date(s: str) -> Any:
    from datetime import date

    return date.fromisoformat(s)


# ---------------------------------------------------------------- voice notes


@asset(partitions_def=audio_notes, group_name="voice")
@traced
def audio_file(context: AssetExecutionContext, db: Database, storage: Storage) -> dict[str, Any]:
    note = _row(db, "audio_note", context.partition_key)
    path = storage.fetch(note["uri"])
    return {k: note[k] for k in ("id", "block_id", "jv_id", "lang")} | {"path": str(path)}


@asset(partitions_def=audio_notes, group_name="voice")
@traced
def transcript(
    context: AssetExecutionContext, audio_file: dict[str, Any], models: Models
) -> Transcript:
    """faster-whisper, language auto (restricted to hi / as / en), word timestamps."""
    t = transcribe(Path(audio_file["path"]), models.whisper_model)
    context.add_output_metadata(
        {"language": t.language, "words": len(t.words), "notes": t.notes, "text": t.text[:500]}
    )
    return t


@asset(partitions_def=audio_notes, group_name="voice")
@traced
def lesson_candidates(
    context: AssetExecutionContext,
    audio_file: dict[str, Any],
    transcript: Transcript,
    models: Models,
    db: Database,
    storage: Storage,
) -> list[Lesson]:
    """LLM lessons {formation, hazard, depth range, what happened, what worked} + rule checks."""
    with db.connect() as conn:
        known = formation_names(conn)
    lessons = extract_lessons(
        transcript, models.llm(db, storage.dir()), known, audio_note_id=audio_file["id"]
    )
    if lessons is None:
        raise RuntimeError("no LLM configured (LLM_BACKEND) or it never returned valid lessons")
    context.add_output_metadata(
        {"lessons": len(lessons), "failing_rules": sum(not x.ok for x in lessons)}
    )
    return lessons


@asset(partitions_def=audio_notes, group_name="voice")
@traced
def stored_lessons(
    audio_file: dict[str, Any],
    transcript: Transcript,
    lesson_candidates: list[Lesson],
    db: Database,
    models: Models,
) -> MaterializeResult[Any]:
    """Lessons with status pending_confirmation; provenance = audio note + time span."""
    version = f"voice@1.0.0/{transcript.model}/extract_lessons@{ASR_VERSION}/{models.llm_model}"
    with db.connect() as conn:
        counts = store_lessons(conn, audio_file, lesson_candidates, version)
    return MaterializeResult(metadata=counts)


# ---------------------------------------------------------------- live top re-anchoring


@asset(group_name="depth")
def reanchored_hazards(context: AssetExecutionContext, db: Database) -> MaterializeResult[Any]:
    """Re-project formation-relative hazards of every wellbore with a newer human top pick
    (picks made through pipelines.reanchor.pick_top are re-projected immediately)."""
    moved = 0
    with db.connect() as conn:
        stale = stale_wellbores(conn)
        for wb in stale:
            with conn.transaction():
                moved += sum(c.after != c.before for c in reanchor_wellbore(conn, wb))
    return MaterializeResult(metadata={"wellbores": len(stale), "hazards_moved": moved})


# ---------------------------------------------------------------- public wells (Sodir)


class Sodir(ConfigurableResource):  # type: ignore[type-arg]
    reports_per_run: int = 3  # wellbores whose completion reports are fetched per run
    max_report_mb: int = 60


@asset(group_name="public_wells")
def public_wells(
    context: AssetExecutionContext, db: Database, storage: Storage, sodir_cfg: Sodir
) -> MaterializeResult[Any]:
    """Sodir FactPages: upsert every exploration wellbore, then fetch the completion reports
    of the newest unchecked ones into the object store as documents (see pipelines/sodir.py)."""
    from minio import Minio

    wells = sodir.wellbores(sodir.table("wellbore_exploration_all"))
    e = urlsplit(storage.endpoint)
    s3 = Minio(e.netloc, storage.access_key, storage.secret_key, secure=e.scheme == "https")
    if not s3.bucket_exists("documents"):
        s3.make_bucket("documents")

    def put(key: str, path: Path, sha: str) -> None:
        s3.fput_object("documents", key, str(path), metadata={"sha256": sha})

    with db.connect() as conn:
        new, total = sodir.upsert(conn, wells)
        conn.commit()
        titles: list[str] = []
        if sodir_cfg.reports_per_run > 0:
            reports = sodir.completion_reports(sodir.table("wellbore_document"))
            titles = sodir.fetch_reports(
                conn, reports, put, sodir_cfg.reports_per_run, sodir_cfg.max_report_mb * 1024
            )
            conn.commit()
    context.log.info(f"sodir: {total} wellbores ({new} new), {len(titles)} reports registered")
    return MaterializeResult(
        metadata={"wellbores": total, "new_wellbores": new, "reports": len(titles)}
    )


# ---------------------------------------------------------------- jobs, sensors, resources

document_job = define_asset_job(
    "document_job",
    selection=AssetSelection.assets(
        document_file,
        document_type,
        page_images,
        page_layout,
        extraction_passes,
        validated_extraction,
        stored_facts,
    ),
)
voice_job = define_asset_job(
    "voice_job",
    selection=AssetSelection.assets(audio_file, transcript, lesson_candidates, stored_lessons),
)
ddr_job = define_asset_job("ddr_events_job", selection=AssetSelection.assets(ddr_events))
ddr_schedule = ScheduleDefinition(job=ddr_job, cron_schedule="0 * * * *")
reanchor_job = define_asset_job("reanchor_job", selection=AssetSelection.assets(reanchored_hazards))
public_wells_job = define_asset_job(
    "public_wells_job", selection=AssetSelection.assets(public_wells)
)
# Daily at 05:00 UTC (Sodir refreshes its tables overnight). SODIR_SYNC=0 where there is no
# route to factpages.sodir.no.
public_wells_schedule = ScheduleDefinition(
    job=public_wells_job,
    cron_schedule="0 5 * * *",
    default_status=DefaultScheduleStatus.RUNNING
    if os.environ.get("SODIR_SYNC", "1") == "1"
    else DefaultScheduleStatus.STOPPED,
)


@sensor(job=reanchor_job, minimum_interval_seconds=60)
def top_picks(context: SensorEvaluationContext, db: Database) -> SensorResult:
    """A human top pick newer than its well's hazards: re-anchor them."""
    with db.connect() as conn:
        stale = stale_wellbores(conn)
    if not stale:
        return SensorResult(run_requests=[])
    return SensorResult(run_requests=[RunRequest(run_key=f"reanchor-{'-'.join(map(str, stale))}")])


def _new_ids(context: SensorEvaluationContext, db: Database, table: str, name: str) -> list[str]:
    with db.connect() as conn:
        ids = [str(r[0]) for r in conn.execute(f"SELECT id FROM {table} ORDER BY id")]
    return [i for i in ids if not context.instance.has_dynamic_partition(name, i)]


@sensor(job=document_job, minimum_interval_seconds=60)
def new_documents(context: SensorEvaluationContext, db: Database) -> SensorResult:
    new = _new_ids(context, db, "document", "document")
    return SensorResult(
        run_requests=[RunRequest(partition_key=k, run_key=f"document-{k}") for k in new],
        dynamic_partitions_requests=[documents.build_add_request(new)] if new else [],
    )


@sensor(job=voice_job, minimum_interval_seconds=60)
def new_audio_notes(context: SensorEvaluationContext, db: Database) -> SensorResult:
    new = _new_ids(context, db, "audio_note", "audio_note")
    return SensorResult(
        run_requests=[RunRequest(partition_key=k, run_key=f"audio-{k}") for k in new],
        dynamic_partitions_requests=[audio_notes.build_add_request(new)] if new else [],
    )


def resources_from_env() -> dict[str, Any]:
    env = os.environ.get
    return {
        "db": Database(url=env("DATABASE_URL", "")),
        "storage": Storage(
            endpoint=env("S3_ENDPOINT", ""),
            access_key=env("S3_ACCESS_KEY", ""),
            secret_key=env("S3_SECRET_KEY", ""),
            work_dir=env("PIPELINE_WORK_DIR", "/opt/dagster/work"),
        ),
        "models": Models(
            llm_backend=env("LLM_BACKEND", "none"),
            llm_url=env("LLM_URL", env("OLLAMA_URL", "http://ollama:11434")),
            llm_model=env("LLM_MODEL", "qwen2.5:7b-instruct"),
            vlm_model=env("VLM_MODEL", "qwen2.5vl:7b"),
            whisper_model=env("WHISPER_MODEL", "small"),
            confidence_threshold=float(env("CONFIDENCE_THRESHOLD", "0.8")),
            second_pass=env("SECOND_PASS", "auto"),
            layout_engines=env("LAYOUT_ENGINES", "text_layer,docling,paddleocr"),
        ),
        "sodir_cfg": Sodir(
            reports_per_run=int(env("SODIR_REPORTS_PER_RUN", "3")),
            max_report_mb=int(env("SODIR_MAX_REPORT_MB", "60")),
        ),
    }


ALL_ASSETS = [
    document_file,
    document_type,
    page_images,
    page_layout,
    extraction_passes,
    validated_extraction,
    stored_facts,
    ddr_events,
    audio_file,
    transcript,
    lesson_candidates,
    stored_lessons,
    reanchored_hazards,
    public_wells,
]
