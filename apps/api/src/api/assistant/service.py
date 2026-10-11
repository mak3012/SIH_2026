"""The assistant pipeline:

question -> refusal rules -> entities (+ previous turn) -> intent
         -> evidence: rule-planned SQL over chat.v_* (app_ro, row limit, timeout, RLS)
                      | model-written SQL, guarded (only when no rule applies)
                      + hybrid retrieval over passages (explain, or no records)
         -> draft: the LLM from evidence only, or the rule composer without an LLM
         -> number verifier: every number, depth and date matches cited evidence;
            otherwise one regeneration with the problems, then "not in the records"
         -> answer + citations (document page bbox / audio span / query) + evidence payload
            + follow-ups
"""

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from api.assistant import safety, sqlguard
from api.assistant.entities import Entities, Lexicon, inherit, intent
from api.assistant.evidence import Evidence, EvidenceSet, resolve_citations
from api.assistant.followups import suggest
from api.assistant.planner import Query, compose, plan, to_evidence
from api.assistant.retrieval import Embedder, HashEmbedder, search
from api.assistant.verify import CITE, verify
from api.auth import User
from api.db import scoped
from core.llm import LLM, PromptSet
from core.telemetry import VERIFIER_REJECTS, tracer

log = logging.getLogger(__name__)
PROMPTS = PromptSet(
    Path(__file__).resolve().parents[1] / "prompts",
    {"assistant_answer": "v2", "assistant_sql": "v1"},
)
NOT_IN_RECORDS = "Not in the records: I found no evidence that answers this."
NOT_VISIBLE = "Not in the records: there is no well named {} in the records you can access."
UNVERIFIED = (
    "Not in the records: I could not write an answer whose numbers all match the cited "
    "evidence. The evidence I found is attached."
)
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "string"}},
        "not_in_records": {"type": "boolean"},
    },
    "required": ["answer", "citations", "not_in_records"],
}
SQL_SCHEMA = {
    "type": "object",
    "properties": {"sql": {"type": "string"}, "params": {"type": "object"}},
    "required": ["sql", "params"],
}


@dataclass
class Draft:
    """What a drafter gets: the question and the evidence, nothing else."""

    question: str
    intent: str
    entities: Entities
    evidence: EvidenceSet
    records: dict[str, list[Evidence]]
    passages: list[Evidence]
    history: list[tuple[str, str]]


@dataclass
class Result:
    status: str  # answered | not_in_records | refused
    intent: str | None
    answer: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)
    entities: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] = field(default_factory=dict)
    refusal: str | None = None
    drafter: str | None = None  # llm | rules
    model: str | None = None
    latency_ms: int = 0

    def json(self) -> dict[str, Any]:
        return asdict(self)


class Assistant:
    def __init__(
        self,
        connect: Callable[[], psycopg.Connection],
        llm: LLM | None = None,
        embedder: Embedder | None = None,
        drafter: "Callable[[Draft, str, int], dict[str, Any] | None] | None" = None,
    ) -> None:
        """`drafter` replaces the LLM drafter (evaluation: a drafter that makes mistakes)."""
        self.connect, self.llm, self.drafter = connect, llm, drafter
        self.embedder = embedder or HashEmbedder()
        self._schema: str | None = None

    @property
    def use_llm(self) -> bool:
        return self.llm is not None and self.llm.enabled

    # ------------------------------------------------------------------ public
    def ask(
        self,
        user: User,
        question: str,
        previous: dict[str, Any] | None = None,
        history: list[tuple[str, str]] | None = None,
    ) -> Result:
        t0 = time.monotonic()
        question = question.strip()[:2000]
        with tracer.start_as_current_span("assistant.ask") as span, self.connect() as conn:
            res = self._ask(conn, user, question, previous, history or [])
            span.set_attributes(
                {
                    "assistant.status": res.status,
                    "assistant.intent": res.intent or "",
                    "assistant.drafter": res.drafter or "",
                    "assistant.citations": len(res.citations),
                }
            )
        res.latency_ms = int((time.monotonic() - t0) * 1000)
        return res

    # ------------------------------------------------------------------ steps
    @contextmanager
    def _scope(
        self, conn: psycopg.Connection, user: User, role: str
    ) -> Iterator[psycopg.Connection]:
        with scoped(conn, role, user.blocks, user.jvs):
            yield conn

    def _ask(
        self,
        conn: psycopg.Connection,
        user: User,
        q: str,
        previous: dict[str, Any] | None,
        history: list[tuple[str, str]],
    ) -> Result:
        with self._scope(conn, user, "app_rw"):
            lex = Lexicon.load(conn)
        ents = lex.parse(q)
        refused = safety.check(q, ents.any())
        if refused:
            return Result(
                "refused",
                None,
                refused[1],
                refusal=refused[0],
                entities=ents.json(),
                followups=suggest("refused", ents, {}),
            )
        if ents.unknown_wells:
            return Result(
                "not_in_records",
                None,
                NOT_VISIBLE.format(", ".join(ents.unknown_wells)),
                entities=ents.json(),
                followups=suggest("not_in_records", ents, {}),
            )
        ents = inherit(ents, q, previous)
        kind = intent(q)

        queries = plan(kind, ents)
        errors: list[str] = []
        with self._scope(conn, user, "app_ro"):
            for qy in queries:
                qy.rows = self._run(conn, qy, errors)
            if not queries and self.use_llm and kind != "explain":
                mq = self._llm_sql(conn, q, errors)
                if mq is not None:
                    queries.append(mq)
        ev = EvidenceSet()
        records = to_evidence(ev, queries)
        passages: list[Evidence] = []
        with self._scope(conn, user, "app_rw"):
            # Passages explain; they never stand in for a structured query that found nothing
            # ("the records were queried and have no such row"). Without an explain intent a
            # passage must share most of the question's own words, not just the well name.
            if kind == "explain" or not queries:
                for h in search(conn, self.embedder, q, ents):
                    if h.score > 0.15 and (kind == "explain" or h.overlap >= 0.5):
                        passages.append(
                            ev.add(
                                "passage",
                                h.text,
                                source_ref_id=h.source_ref_id,
                                score=round(h.score, 3),
                            )
                        )
            resolve_citations(conn, ev.items)
        protected = [*lex.wells, *lex.formations, *_titles(ev)]
        if not ev.items:
            return Result(
                "not_in_records",
                kind,
                NOT_IN_RECORDS,
                entities=ents.json(),
                verification={"queries": _qlog(queries), "errors": errors},
                followups=suggest("not_in_records", ents, {}),
            )

        attempts: list[list[str]] = []
        answer, cited_ok = "", False
        draft_in = Draft(q, kind, ents, ev, records, passages, history)
        drafter = "custom" if self.drafter else ("llm" if self.use_llm else "rules")
        if drafter == "rules":
            text = compose(q, kind, ents, ev, records, passages)
            problems = verify(text, ev, protected) if text else ["nothing to compose from"]
            attempts.append(problems)
            answer, cited_ok = text, bool(text) and not problems
        else:
            feedback = ""
            for attempt in range(2):
                out = (
                    self.drafter(draft_in, feedback, attempt)
                    if self.drafter
                    else self._llm_answer(q, history, ev, feedback)
                )
                if out is None:
                    attempts.append(["no valid output from the model"])
                    break
                if out.get("not_in_records"):
                    attempts.append([])
                    break
                problems = verify(out["answer"], ev, protected)
                attempts.append(problems)
                if problems:
                    VERIFIER_REJECTS.labels("assistant").inc()
                else:
                    answer, cited_ok = out["answer"].strip(), True
                    break
                feedback = (
                    "Your previous answer failed verification: "
                    + "; ".join(problems[:8])
                    + ". Use only numbers from the cited evidence, and cite every sentence "
                    "that has a number."
                )

        if not cited_ok and drafter == "llm":
            # The model failed verification twice or did not answer (down, timed out): fall
            # back to the rule-composed answer, verified the same way, as briefings do.
            text = compose(q, kind, ents, ev, records, passages)
            problems = verify(text, ev, protected) if text else ["nothing to compose from"]
            attempts.append(problems)
            if text and not problems:
                answer, cited_ok, drafter = text, True, "rules"
        verification = {
            "attempts": attempts,
            "verified": cited_ok,
            "queries": _qlog(queries),
            "errors": errors,
        }
        payload = [e.payload() for e in ev.items]
        if not cited_ok:
            msg = UNVERIFIED if any(attempts) else NOT_IN_RECORDS
            return Result(
                "not_in_records",
                kind,
                msg,
                evidence=payload,
                verification=verification,
                drafter=drafter,
                model=self._model(),
                entities=ents.json(),
                followups=suggest("not_in_records", ents, records),
            )
        ids = list(dict.fromkeys(CITE.findall(answer)))
        cited = ev.closure(ids)
        if any((e.citation or {}).get("synthetic") for e in cited):
            answer += " (These records are SYNTHETIC test data.)"
        return Result(
            "answered",
            kind,
            answer,
            citations=[_citation(e, e.id in ids) for e in cited],
            evidence=payload,
            followups=suggest(kind, ents, records),
            verification=verification,
            drafter=drafter,
            model=self._model(),
            entities=ents.json(),
        )

    def _run(self, conn: psycopg.Connection, qy: Query, errors: list[str]) -> list[dict[str, Any]]:
        try:
            sql = sqlguard.check(qy.sql, qy.params)
            with conn.cursor(row_factory=dict_row) as cur, conn.transaction():  # savepoint
                cur.execute(sql, qy.params)
                return list(cur.fetchall())
        except (sqlguard.UnsafeSQL, psycopg.Error) as e:
            errors.append(f"{qy.purpose}: {type(e).__name__}: {str(e)[:200]}")
            return []

    def _model(self) -> str | None:
        return self.llm.model_version(self.llm.cfg.model) if self.use_llm and self.llm else None

    # ------------------------------------------------------------------ model calls
    def schema(self, conn: psycopg.Connection) -> str:
        if self._schema is None:
            rows = conn.execute(
                "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod),"
                " col_description(c.oid, a.attnum) FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0"
                " WHERE n.nspname = 'chat' AND c.relkind = 'v' AND c.relname LIKE 'v\\_%%'"
                " ORDER BY c.relname, a.attnum"
            ).fetchall()
            lines, last = [], None
            for view, col, typ, doc in rows:
                if view != last:
                    lines.append(f"chat.{view}:")
                    last = view
                lines.append(f"  {col} {typ}" + (f" -- {doc}" if doc else ""))
            self._schema = "\n".join(lines)
        return self._schema

    def _llm_sql(self, conn: psycopg.Connection, q: str, errors: list[str]) -> Query | None:
        assert self.llm is not None
        feedback = ""
        for _ in range(2):
            out = self.llm.complete_json(
                "assistant_sql",
                {"question": q, "schema": self.schema(conn), "feedback": feedback},
                SQL_SCHEMA,
            )
            if not out:
                return None
            qy = Query("model_sql", str(out["sql"]), dict(out.get("params") or {}))
            rows = self._run(conn, qy, errors)
            if rows or not errors:
                qy.rows = rows
                return qy
            feedback = f"Your SQL was refused or failed: {errors[-1]}. Fix it."
        return None

    def _llm_answer(
        self, q: str, history: list[tuple[str, str]], ev: EvidenceSet, feedback: str
    ) -> dict[str, Any] | None:
        assert self.llm is not None
        lines = []
        for e in ev.items:
            src = e.citation or {}
            where = (
                f"{src.get('document')} p.{src.get('page')}"
                if src.get("type") == "document"
                else f"voice note {src.get('t_start')}-{src.get('t_end')} s"
                if src.get("type") == "audio"
                else (e.query or {}).get("view", "")
            )
            vals = {k: v for k, v in e.payload()["values"].items() if v is not None}
            lines.append(
                f"[{e.id}] ({e.kind}; {where}) {e.text}" + (f" | values: {vals}" if vals else "")
            )
        hist = "\n".join(f"Q: {a}\nA: {b}" for a, b in history[-3:]) or "(none)"
        out = self.llm.complete_json(
            "assistant_answer",
            {"question": q, "history": hist, "evidence": "\n".join(lines), "feedback": feedback},
            ANSWER_SCHEMA,
        )
        return out if isinstance(out, dict) and isinstance(out.get("answer"), str) else None


def _titles(ev: EvidenceSet) -> list[str]:
    out = []
    for e in ev.items:
        c = e.citation or {}
        out += [str(c[k]) for k in ("document", "uri") if c.get(k)]
    return out


def _qlog(queries: list[Query]) -> list[dict[str, Any]]:
    return [
        {"purpose": q.purpose, "sql": q.sql, "params": q.params, "rows": len(q.rows)}
        for q in queries
    ]


def _citation(e: Evidence, direct: bool) -> dict[str, Any]:
    c = dict(e.citation or {})
    out: dict[str, Any] = {"id": e.id, "kind": e.kind, "direct": direct, **c}
    if not c and e.query:
        out.update(
            {
                "type": "query",
                "view": e.query["view"],
                "sql": e.query["sql"],
                "params": e.query["params"],
            }
        )
    if e.kind == "aggregate":
        out.update({"type": "aggregate", "derived_from": e.derived_from, "text": e.text})
    return out
