"""Evidence items: what an answer may cite. Each has an id (E1, E2, ...), the values a
number in the answer may match, and where it came from: a source (document page + bbox, or audio
note + time span) and/or the query that produced it."""

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg


@dataclass
class Evidence:
    id: str
    kind: str  # record (a view row) | passage (retrieved text) | aggregate (computed from records)
    text: str
    values: dict[str, Any] = field(default_factory=dict)
    source_ref_id: int | None = None
    citation: dict[str, Any] | None = None  # resolved source_ref
    query: dict[str, Any] | None = None  # {"view", "sql", "params"}
    derived_from: list[str] = field(default_factory=list)
    score: float | None = None  # retrieval / rerank score

    def payload(self) -> dict[str, Any]:
        d = asdict(self)
        d["values"] = {k: _json(v) for k, v in self.values.items()}
        return d


def _json(v: Any) -> Any:
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, list | tuple):
        return [_json(x) for x in v]
    return v


class EvidenceSet:
    def __init__(self) -> None:
        self.items: list[Evidence] = []

    def add(self, kind: str, text: str, **kw: Any) -> Evidence:
        e = Evidence(f"E{len(self.items) + 1}", kind, text, **kw)
        self.items.append(e)
        return e

    def get(self, eid: str) -> Evidence | None:
        return next((e for e in self.items if e.id == eid), None)

    def closure(self, ids: list[str]) -> list[Evidence]:
        """Cited items plus what aggregates were computed from."""
        out: dict[str, Evidence] = {}
        todo = list(ids)
        while todo:
            e = self.get(todo.pop())
            if e and e.id not in out:
                out[e.id] = e
                todo += e.derived_from
        return list(out.values())

    def __len__(self) -> int:
        return len(self.items)


def resolve_citations(conn: psycopg.Connection, items: list[Evidence]) -> None:
    """Fill `citation` from source_ref under the caller's RLS scope (rows the caller may not
    see stay unresolved and are dropped by the caller)."""
    ids = sorted({e.source_ref_id for e in items if e.source_ref_id is not None})
    if not ids:
        return
    rows = conn.execute(
        "SELECT r.id, d.id, d.title, d.synthetic, r.page, r.bbox, a.id, a.uri, a.synthetic,"
        " r.t_start, r.t_end, r.review_status::text FROM source_ref r"
        " LEFT JOIN document d ON d.id = r.document_id"
        " LEFT JOIN audio_note a ON a.id = r.audio_note_id WHERE r.id = ANY(%s)",
        (ids,),
    ).fetchall()
    by_id: dict[int, dict[str, Any]] = {}
    for rid, did, title, dsyn, page, bbox, aid, uri, asyn, t0, t1, review in rows:
        if did is not None:
            by_id[rid] = {
                "type": "document",
                "source_ref_id": rid,
                "document_id": did,
                "document": title,
                "page": page,
                "bbox": bbox,
                "synthetic": dsyn,
                "review_status": review,
            }
        elif aid is not None:
            by_id[rid] = {
                "type": "audio",
                "source_ref_id": rid,
                "audio_note_id": aid,
                "uri": uri,
                "t_start": t0,
                "t_end": t1,
                "synthetic": asyn,
                "review_status": review,
            }
    for e in items:
        if e.source_ref_id is not None:
            e.citation = by_id.get(e.source_ref_id)
