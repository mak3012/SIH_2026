"""One document through the pipeline: classify -> layout (engines per page) -> two extraction
passes -> rules -> confidence -> accept or review.

confidence(field) = min(ocr, consistency, rules)
  ocr          recognition confidence of the cells the value came from (text layer = 1.0)
  consistency  1.0 both passes agree; 0.0 they disagree; `single_pass` when only one pass
               produced the value (default 0.5, so single-pass values always get a human look)
  rules        1.0 no rule failed; 0.5 only soft rules failed; 0.0 a hard rule failed
A field is accepted when confidence >= threshold (default 0.8); otherwise it goes to review.
"""

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pipelines import ocr
from pipelines.classify import classify
from pipelines.extract import PARSERS, Extraction, Value
from pipelines.layout import Cell, PageLayout, deskew, render_pages, rotate_bbox, text_layer
from pipelines.llm import LLM, PROMPT_VERSIONS
from pipelines.norm import same
from pipelines.rules import Failure, validate
from pipelines.schemas import SCHEMAS, flatten

EXTRACTOR_VERSION = "1.0.0"


@dataclass
class Settings:
    threshold: float = 0.8
    single_pass: float = 0.5
    vlm_conf: float = 0.85
    ocr_min_conf: float = 0.80
    engines: tuple[str, ...] = ("text_layer", "docling", "paddleocr")
    second_pass: str = "auto"  # auto | llm | ocr | none
    dpi: int = 300

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.threshold = float(os.environ.get("CONFIDENCE_THRESHOLD", s.threshold))
        s.engines = tuple(os.environ.get("LAYOUT_ENGINES", ",".join(s.engines)).split(","))
        s.second_pass = os.environ.get("SECOND_PASS", s.second_pass)
        return s


@dataclass
class FieldResult:
    name: str
    value: Any
    printed: str
    page: int
    bbox: list[float]
    components: dict[str, float]
    confidence: float
    accepted: bool
    failures: list[str]
    source: str
    conversions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DocResult:
    doc_type: str
    class_conf: float
    depth_unit: str | None
    fields: dict[str, FieldResult]
    failures: list[Failure]
    engines: list[str]
    passes: list[str]
    extractor_version: str
    notes: list[str] = field(default_factory=list)

    def review(self) -> list[FieldResult]:
        return [f for f in self.fields.values() if not f.accepted]

    def accepted(self) -> list[FieldResult]:
        return [f for f in self.fields.values() if f.accepted]

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["failures"] = [asdict(f) for f in self.failures]
        return d


def _cached(image: Path, engine: str, n: int) -> PageLayout:
    """OCR result per (image, engine), kept next to the image: reruns do not re-OCR."""
    cache = image.with_name(f"{image.stem}.{engine}.json")
    if cache.exists() and cache.stat().st_mtime >= image.stat().st_mtime:
        d = json.loads(cache.read_text())
        return PageLayout(n, [Cell(**c) for c in d["cells"]], engine, False, str(image))
    page = ocr.docling_image(image, n) if engine == "docling" else ocr.paddle_layout(image, n)
    cache.write_text(json.dumps({"cells": [asdict(c) for c in page.cells]}))
    return page


def ocr_page(image: Path, n: int, s: Settings, skip: tuple[str, ...] = ()) -> PageLayout:
    """First engine, in configured order, that reads anything on this (deskewed) page image."""
    for engine in s.engines:
        if engine in skip or engine == "text_layer" or not ocr.available(engine):
            continue
        page = _cached(image, engine, n)
        if page.cells:
            return page
    return PageLayout(n, [], "none", False, str(image))


def layout(
    pdf: Path, s: Settings, llm: LLM | None, cache: Path, doc_type_hint: str | None, **log_ctx: Any
) -> tuple[list[PageLayout], list[str]]:
    """Best available layout per page: text layer; else deskew and OCR (Docling, then
    PaddleOCR); VLM for hard pages (handwriting, low OCR confidence, unreadable)."""
    notes: list[str] = []
    pages = text_layer(pdf) if "text_layer" in s.engines else []
    if pages and all(p.has_text_layer for p in pages):
        return pages, notes
    images = render_pages(pdf, cache, s.dpi)
    by_page = {p.page: p for p in pages if p.has_text_layer}
    for n in range(1, len(images) + 1):
        if n in by_page:
            continue
        img, angle = deskew(images[n - 1])
        page = ocr_page(img, n, s)
        hard = not page.cells or page.mean_conf < s.ocr_min_conf or doc_type_hint == "field_note"
        if hard and llm is not None and llm.enabled:
            page = ocr.vlm_layout(img, n, llm, s.vlm_conf, **log_ctx)
        elif not page.cells:
            notes.append(f"page {n}: no engine could read it")
        page.angle, page.image = angle, str(images[n - 1])
        by_page[n] = page
    return [by_page[k] for k in sorted(by_page)], notes


MAIN_TABLE = {"wcr": "tops[", "ddr": "time_breakdown[", "survey_report": "survey["}


def quality(x: Extraction) -> int:
    return sum(v.error is None for v in x.values.values()) - 2 * sum(
        v.error is not None for v in x.values.values()
    )


def weak(doc_type: str, x: Extraction) -> bool:
    key = MAIN_TABLE.get(doc_type)
    return key is not None and not any(n.startswith(key) for n in x.values)


def to_page_coords(x: Extraction, pages: list[PageLayout]) -> Extraction:
    """Boxes found on deskewed images, rotated back onto the original page."""
    angle = {p.page: p.angle for p in pages}
    for v in x.values.values():
        a = angle.get(v.page, 0.0)
        if a:
            v.bbox = rotate_bbox(v.bbox, -a)
    return x


def page_text(pages: list[PageLayout]) -> str:
    return "\n".join(
        f"[page {p.page}]\n" + "\n".join(" | ".join(c.text for c in r) for r in p.rows())
        for p in pages
    )


def ground(printed: str, pages: list[PageLayout]) -> tuple[int, list[float], float]:
    """Where an LLM-reported string is printed: exact cell, then containing cell."""
    t = " ".join(str(printed).split()).lower()
    for exact in (True, False):
        for p in pages:
            for c in p.cells:
                ct = " ".join(c.text.split()).lower()
                if (ct == t) if exact else (t and t in ct):
                    return p.page, c.bbox, c.conf
    return 1, [0.0, 0.0, 1.0, 1.0], 0.0


def llm_pass(doc_type: str, pages: list[PageLayout], llm: LLM, **log_ctx: Any) -> Extraction | None:
    model = SCHEMAS[doc_type]
    out = llm.complete_json(
        f"extract_{doc_type}",
        {"pages": page_text(pages)},
        model.model_json_schema(by_alias=True),
        validate=model.model_validate,
        **log_ctx,
    )
    if out is None:
        return None
    x = Extraction(doc_type, getattr(out, "depth_unit", None), source="llm")
    for name, printed in flatten(out).items():
        page, bbox, conf = ground(
            printed if isinstance(printed, str) else "; ".join(printed), pages
        )
        x.add(name, printed, page, bbox, conf)
    return x


def passes(
    doc_type: str,
    pages: list[PageLayout],
    pdf: Path,
    s: Settings,
    llm: LLM | None,
    cache: Path,
    **log_ctx: Any,
) -> tuple[Extraction, Extraction | None, list[PageLayout]]:
    """Pass A (template parser) and, when available, pass B (LLM, or parser on OCR). If Docling's
    layout leaves the document's main table unparsed, its pages are re-read with PaddleOCR and
    the better parse is kept (the PaddleOCR fallback)."""
    a = PARSERS[doc_type](pages)
    if weak(doc_type, a) and any(p.engine == "docling" for p in pages):
        alt = [
            ocr_page(
                Path(str(p.image).replace(".png", "_deskew.png"))
                if p.angle
                else Path(str(p.image)),
                p.page,
                s,
                skip=("docling",),
            )
            if p.engine == "docling"
            else p
            for p in pages
        ]
        for new, old in zip(alt, pages, strict=True):
            new.angle, new.image = old.angle, old.image
        a2 = PARSERS[doc_type](alt)
        if quality(a2) > quality(a):
            a, pages = a2, alt
    b: Extraction | None = None
    mode = s.second_pass if s.second_pass != "auto" else ("llm" if llm and llm.enabled else "none")
    if mode == "llm" and llm is not None:
        b = llm_pass(doc_type, pages, llm, **log_ctx)
    elif mode == "ocr" and ocr.available("paddleocr"):
        imgs = render_pages(pdf, cache, s.dpi)
        alt_pages = []
        for i, im in enumerate(imgs):
            img, angle = deskew(im)
            p = ocr.paddle_layout(img, i + 1)
            p.angle = angle
            alt_pages.append(p)
        b = to_page_coords(PARSERS[doc_type](alt_pages), alt_pages)
        b.source = "ocr"
    return to_page_coords(a, pages), b, pages


def decide(
    doc_type: str,
    class_conf: float,
    a: Extraction,
    b: Extraction | None,
    engines: list[str],
    s: Settings,
    context: dict[str, Any] | None = None,
    notes: list[str] | None = None,
) -> DocResult:
    """Rules and confidence for every field; accept or send to review."""
    merged: dict[str, Value] = dict(a.values)
    for n, v in (b.values if b else {}).items():
        merged.setdefault(n, v)
    rule_score, failures = validate(merged, {"doc_type": doc_type, **(context or {})})
    by_field: dict[str, list[str]] = {}
    for f in failures:
        for n in f.fields:
            by_field.setdefault(n, []).append(f"{f.rule}: {f.message}")
    fields = {}
    for n, v in merged.items():
        if b is not None and n in a.values and n in b.values:
            consistency = 1.0 if same(n, a.values[n].value, b.values[n].value) else 0.0
        else:
            consistency = s.single_pass
        comp = {
            "ocr": round(v.ocr_conf, 3),
            "consistency": consistency,
            "rules": rule_score.get(n, 1.0),
        }
        conf = min(comp.values())
        fields[n] = FieldResult(
            n,
            v.value,
            v.printed,
            v.page,
            v.bbox,
            comp,
            conf,
            conf >= s.threshold and v.error is None,
            by_field.get(n, []),
            v.source,
            v.conversions,
        )
    used = ["parser"] + ([b.source] if b else [])
    return DocResult(
        doc_type,
        class_conf,
        a.depth_unit or (b.depth_unit if b else None),
        fields,
        failures,
        engines,
        used,
        _version(engines, used),
        (notes or []) + a.notes,
    )


def process(
    pdf: Path,
    *,
    settings: Settings | None = None,
    llm: LLM | None = None,
    cache: Path | None = None,
    registered_type: str | None = None,
    context: dict[str, Any] | None = None,
    **log_ctx: Any,
) -> DocResult:
    """The whole flow for one document (the Dagster assets run the same steps one by one)."""
    s = settings or Settings()
    cache = cache or pdf.parent / ".pages" / pdf.stem
    hint = "field_note" if "_NOTE_" in pdf.name.upper() else None
    pages, notes = layout(pdf, s, llm, cache, hint, **log_ctx)
    doc_type, class_conf, _ = classify(page_text(pages[:2]), pdf.name, registered_type)
    engines = sorted({p.engine for p in pages})
    if doc_type not in PARSERS:
        return unsupported(doc_type, class_conf, engines, notes)
    a, b, pages = passes(doc_type, pages, pdf, s, llm, cache, **log_ctx)
    engines = sorted({p.engine for p in pages})
    return decide(doc_type, class_conf, a, b, engines, s, context, notes)


def unsupported(
    doc_type: str, class_conf: float, engines: list[str], notes: list[str]
) -> DocResult:
    return DocResult(
        doc_type,
        class_conf,
        None,
        {},
        [Failure("R-CLASSIFY", "hard", [], f"unsupported or unknown document type ({doc_type})")],
        engines,
        [],
        _version(engines, []),
        notes,
    )


def _version(engines: list[str], passes: list[str]) -> str:
    llm = "+".join(f"{k}@{v}" for k, v in PROMPT_VERSIONS.items() if k.startswith("extract_"))
    return f"extract@{EXTRACTOR_VERSION}/{'+'.join(engines)}/{'+'.join(passes) or 'none'}" + (
        f"/{llm}" if "llm" in passes else ""
    )
