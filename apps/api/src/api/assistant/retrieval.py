"""Hybrid retrieval over `chunk`: Postgres full-text search + pgvector cosine, fused by
reciprocal rank, then reranked.

    fts     ts_rank_cd(tsv, to_tsquery(term1 | term2 | ...))           top 30
    vector  embedding <=> query_embedding (same embedding model only)   top 30
    fused   sum over the two lists of 1 / (60 + rank)
    rerank  0.40 fused (normalised) + 0.25 term overlap + 0.20 well named
            + 0.10 formation / hazard named + 0.05 depth within 2 %

Runs under the caller's RLS scope: chunks resolve through source_ref to documents and audio
notes of the caller's blocks only.

Embeddings: `ollama` (EMBED_MODEL, default bge-m3, 1024-d) or `hash` (feature hashing of words
and word pairs, 1024-d; offline, deterministic, weaker - the default when no LLM backend is set).
`python -m api.assistant index` fills missing embeddings.

ponytail: rule reranker. A cross-encoder (e.g. bge-reranker) fits behind `rerank()` when real
questions show the rules ordering passages badly.
"""

import hashlib
import json
import math
import os
import re
import urllib.request
from dataclasses import dataclass
from typing import Protocol

import psycopg

from api.assistant.entities import HAZARDS, Entities

DIM = 1024
STOP = set(
    "a an the of in on at to for from by with and or is was were be been what which who whom "
    "when where why how did does do had has have it its this that these those there their "
    "well wells me tell show give please about any all".split()
)


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+(?:[-/][a-z0-9]+)*", text.lower()) if w not in STOP]


class HashEmbedder:
    name = "hash-1024@1"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * DIM
            ws = words(t)
            for f in ws + [f"{a} {b}" for a, b in zip(ws, ws[1:], strict=False)]:
                h = hashlib.blake2b(f.encode(), digest_size=8).digest()
                i = int.from_bytes(h[:4], "little") % DIM
                v[i] += 1.0 if h[4] & 1 else -1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


class OllamaEmbedder:
    def __init__(self, url: str, model: str) -> None:
        self.url, self.model = url.rstrip("/"), model
        self.name = f"ollama:{model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        body = json.dumps({"model": self.model, "input": texts}).encode()
        req = urllib.request.Request(
            f"{self.url}/api/embed", body, {"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            vecs = json.load(r)["embeddings"]
        if any(len(v) != DIM for v in vecs):
            raise ValueError(
                f"{self.model} returns {len(vecs[0])}-d vectors; chunk.embedding is {DIM}-d"
            )
        return [list(map(float, v)) for v in vecs]


def embedder_from_env() -> Embedder:
    backend = os.environ.get("EMBED_BACKEND") or (
        "ollama" if os.environ.get("LLM_BACKEND") == "ollama" else "hash"
    )
    if backend == "ollama":
        url = os.environ.get("LLM_URL") or os.environ.get("OLLAMA_URL", "http://localhost:11434")
        return OllamaEmbedder(url, os.environ.get("EMBED_MODEL", "bge-m3"))
    return HashEmbedder()


def vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def index(conn: psycopg.Connection, emb: Embedder, batch: int = 64) -> int:
    """Embed every chunk without an embedding from this model (owner connection)."""
    done = 0
    while True:
        rows = conn.execute(
            "SELECT id, text FROM chunk WHERE embedding IS NULL OR embedding_model IS DISTINCT"
            " FROM %s ORDER BY id LIMIT %s",
            (emb.name, batch),
        ).fetchall()
        if not rows:
            return done
        for (cid, _), v in zip(rows, emb.embed([t for _, t in rows]), strict=True):
            conn.execute(
                "UPDATE chunk SET embedding = %s::vector, embedding_model = %s WHERE id = %s",
                (vec(v), emb.name, cid),
            )
        conn.commit()
        done += len(rows)


@dataclass
class Hit:
    chunk_id: int
    source_ref_id: int
    text: str
    fts_rank: int | None
    vec_rank: int | None
    fused: float
    score: float = 0.0
    overlap: float = 0.0  # share of the question's content words (not entity names) in the text


def search(
    conn: psycopg.Connection, emb: Embedder, query: str, ents: Entities, k: int = 5, pool: int = 30
) -> list[Hit]:
    terms = list(dict.fromkeys(t for w in words(query) for t in re.split(r"[-/]", w) if t))
    hits: dict[int, Hit] = {}
    if terms:
        q = " | ".join(dict.fromkeys(terms))
        rows = conn.execute(
            "SELECT c.id, c.source_ref_id, c.text FROM chunk c, to_tsquery('english', %s) q"
            " WHERE c.tsv @@ q ORDER BY ts_rank_cd(c.tsv, q) DESC, c.id LIMIT %s",
            (q, pool),
        ).fetchall()
        for rank, (cid, sid, text) in enumerate(rows, 1):
            hits[cid] = Hit(cid, sid, text, rank, None, 1 / (60 + rank))
    qv = emb.embed([query])[0]
    rows = conn.execute(
        "SELECT id, source_ref_id, text FROM chunk WHERE embedding_model = %s"
        " ORDER BY embedding <=> %s::vector, id LIMIT %s",
        (emb.name, vec(qv), pool),
    ).fetchall()
    for rank, (cid, sid, text) in enumerate(rows, 1):
        h = hits.setdefault(cid, Hit(cid, sid, text, None, None, 0.0))
        h.vec_rank = rank
        h.fused += 1 / (60 + rank)
    return rerank(list(hits.values()), terms, ents)[:k]


def rerank(hits: list[Hit], terms: list[str], ents: Entities) -> list[Hit]:
    if not hits:
        return []
    top = max(h.fused for h in hits)
    names = {
        t for n in [*ents.wells, *ents.formations] for t in re.split(r"[-/\s]", n.lower()) if t
    }
    qset = {t for t in terms if t not in names and not t.isdigit()}
    for h in hits:
        tw = {t for w in words(h.text) for t in re.split(r"[-/]", w)}
        overlap = len(qset & tw) / len(qset) if qset else 0.0
        h.overlap = overlap
        low = h.text.lower()
        well = (
            1.0
            if any(
                re.search(rf"(?<![\w-]){re.escape(w.lower())}(?![\w-])", low) for w in ents.wells
            )
            else 0.0
        )
        if ents.wells and not well:
            well = -1.0  # a passage about another well is worse than no well at all
        topic = 0.0
        if ents.formations and any(f.lower() in low for f in ents.formations):
            topic += 0.5
        if ents.hazards and any(re.search(rf"\b({HAZARDS[z]})", low) for z in ents.hazards):
            topic += 0.5
        nums = [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", h.text)]
        depth = 1.0 if any(abs(n - d) <= 0.02 * d for d in ents.depths_m for n in nums) else 0.0
        h.score = 0.40 * h.fused / top + 0.25 * overlap + 0.20 * well + 0.10 * topic + 0.05 * depth
    return sorted(hits, key=lambda h: (-h.score, h.chunk_id))
