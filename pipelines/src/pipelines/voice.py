"""Voice notes -> transcript with word timestamps -> lessons pending expert confirmation.

ASR is faster-whisper (optional extra `speech`), language auto-detected but restricted to
Hindi, Assamese and English: if Whisper's best guess is another language, the most likely of the
three is forced. An LLM extracts lessons from the transcript as guided JSON; rules then check
the schema, the formation name against the formation table, the depth range and that the quoted
evidence is really in the transcript. The evidence quote is located in the word stream, which
gives the lesson's provenance: audio note + t_start/t_end.
"""

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.units import ft_to_m
from pipelines.llm import LLM

ALLOWED_LANGUAGES = ("en", "hi", "as")
HAZARDS = (
    "loss",
    "kick",
    "stuck_pipe",
    "tight_hole",
    "pack_off",
    "torque_spike",
    "cement_issue",
    "washout",
    "fishing",
    "other",
)
ASR_VERSION = "1.0.0"


@dataclass
class Word:
    start: float
    end: float
    text: str
    prob: float


@dataclass
class Transcript:
    language: str
    language_prob: float
    text: str
    words: list[Word]
    model: str
    notes: list[str] = field(default_factory=list)


def transcribe(audio: Path, model_size: str = "small", device: str = "cpu") -> Transcript:
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device=device, compute_type="int8")
    notes = []
    segments, info = model.transcribe(
        str(audio),
        language=None,
        word_timestamps=True,
        vad_filter=True,
        temperature=0.0,
        beam_size=5,
    )
    language = info.language
    if language not in ALLOWED_LANGUAGES:
        probs = dict(info.all_language_probs or [])
        forced = max(ALLOWED_LANGUAGES, key=lambda lang: probs.get(lang, 0.0))
        notes.append(f"detected {language} ({info.language_probability:.2f}); forced {forced}")
        language = forced
        segments, info = model.transcribe(
            str(audio),
            language=forced,
            word_timestamps=True,
            vad_filter=True,
            temperature=0.0,
            beam_size=5,
        )
    words = [
        Word(w.start, w.end, w.word.strip(), w.probability)
        for s in segments
        for w in (s.words or [])
    ]
    return Transcript(
        language,
        float(info.language_probability),
        " ".join(w.text for w in words),
        words,
        f"faster-whisper/{model_size}@{ASR_VERSION}",
        notes,
    )


class LessonOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    formation: str | None
    hazard: Literal[
        "loss",
        "kick",
        "stuck_pipe",
        "tight_hole",
        "pack_off",
        "torque_spike",
        "cement_issue",
        "washout",
        "fishing",
        "other",
    ]
    depth_top: float | None
    depth_base: float | None
    depth_unit: Literal["m", "ft"] | None
    what_happened: str
    what_worked: str | None
    evidence: str = Field(description="Exact contiguous words from the transcript")


class LessonsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lessons: list[LessonOut]


@dataclass
class Lesson:
    formation: str | None
    formation_id: int | None
    hazard: str
    md_top: float | None
    md_base: float | None
    what_happened: str
    what_worked: str | None
    evidence: str
    t_start: float | None
    t_end: float | None
    confidence: float
    failures: list[str]

    @property
    def ok(self) -> bool:
        return not self.failures


def locate(quote: str, words: list[Word]) -> tuple[float, float, float] | None:
    """(t_start, t_end, match ratio) of the best matching span of words for a quote."""
    q = _tokens(quote)
    toks = [_tokens(w.text)[0] if _tokens(w.text) else "" for w in words]
    if not q or not toks:
        return None
    best: tuple[float, int, int] = (0.0, 0, 0)
    n = len(q)
    for size in {n - 1, n, n + 1} - {0}:
        for i in range(0, max(1, len(toks) - size + 1)):
            r = difflib.SequenceMatcher(None, q, toks[i : i + size]).ratio()
            if r > best[0]:
                best = (r, i, i + size)
    r, i, j = best
    if r == 0 or j <= i:
        return None
    return words[i].start, words[j - 1].end, r


def _tokens(s: str) -> list[str]:
    return re.findall(r"[\w.]+", s.lower())


def extract_lessons(
    t: Transcript, llm: LLM, formations: dict[str, int], **log_ctx: Any
) -> list[Lesson] | None:
    """None if the LLM is unavailable or never produced valid output."""
    out = llm.complete_json(
        "extract_lessons",
        {
            "transcript": t.text,
            "language": t.language,
            "formations": ", ".join(sorted(formations)) or "(none registered)",
        },
        LessonsOut.model_json_schema(),
        validate=LessonsOut.model_validate,
        **log_ctx,
    )
    if out is None:
        return None
    return [check(x, t, formations) for x in out.lessons]


def check(x: LessonOut, t: Transcript, formations: dict[str, int]) -> Lesson:
    """Rules for a machine-extracted lesson."""
    fails = []
    fid = None
    if x.formation:
        fid = formations.get(x.formation.strip().lower())
        if fid is None:
            fails.append(f"R-FORMATION: {x.formation!r} is not a known formation")
    top, base = x.depth_top, x.depth_base if x.depth_base is not None else x.depth_top
    if top is not None and x.depth_unit is None:
        fails.append("R-UNIT: depth without a unit")
    if x.depth_unit == "ft":
        top = ft_to_m(top) if top is not None else None
        base = ft_to_m(base) if base is not None else None
    if top is not None and not 0 <= top <= 8000:
        fails.append(f"R-RANGE: depth {top:.0f} m outside 0-8000 m")
    if top is not None and base is not None and base < top:
        fails.append("R-ORDER: depth_base above depth_top")
    if not x.what_happened.strip():
        fails.append("R-REQUIRED: what_happened empty")
    span = locate(x.evidence, t.words)
    if span is None or span[2] < 0.6:
        fails.append("R-EVIDENCE: quoted evidence not found in the transcript")
    asr = 0.0
    if span is not None:
        asr = min((w.prob for w in t.words if span[0] <= w.start and w.end <= span[1]), default=0.0)
    rules = 0.0 if fails else 1.0
    return Lesson(
        x.formation,
        fid,
        x.hazard,
        top,
        base,
        x.what_happened,
        x.what_worked,
        x.evidence,
        span[0] if span else None,
        span[1] if span else None,
        round(min(asr, rules), 3),
        fails,
    )


def store_lessons(
    conn: Any, audio: dict[str, Any], lessons: list[Lesson], version: str
) -> dict[str, int]:
    """Valid lessons -> lesson (status pending_confirmation) with an audio source_ref;
    the rest -> review_item. Idempotent per audio note and version."""
    from psycopg.types.json import Jsonb

    counts = {"lessons": 0, "review": 0, "skipped": 0}
    with conn.transaction():
        if conn.execute(
            "SELECT 1 FROM source_ref WHERE audio_note_id = %s AND extractor_version = %s LIMIT 1",
            (audio["id"], version),
        ).fetchone():
            counts["skipped"] = 1
            return counts
        for i, x in enumerate(lessons):
            if not x.ok:
                conn.execute(
                    "INSERT INTO review_item (audio_note_id, kind, field, value, t_start, t_end,"
                    " confidence, components, rule_failures, reason, extractor_version)"
                    " VALUES (%s, 'lesson', %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT DO NOTHING",
                    (
                        audio["id"],
                        f"lessons[{i}]",
                        Jsonb(x.__dict__),
                        x.t_start,
                        x.t_end,
                        x.confidence,
                        Jsonb({"rules": 0.0}),
                        Jsonb(x.failures),
                        "lesson failed validation",
                        version,
                    ),
                )
                counts["review"] += 1
                continue
            src = conn.execute(
                "INSERT INTO source_ref (audio_note_id, t_start, t_end, extractor_version,"
                " confidence) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (audio["id"], x.t_start, x.t_end, version, x.confidence),
            ).fetchone()[0]
            depth = (
                ""
                if x.md_top is None
                else f" at {x.md_top:.0f}"
                + (f"-{x.md_base:.0f}" if x.md_base and x.md_base != x.md_top else "")
                + " m"
            )
            conn.execute(
                "INSERT INTO lesson (title, body, recommendation, hazard, formation_id, status,"
                " author, block_id, jv_id, source_ref_id, md_top, md_base) VALUES (%s, %s, %s, %s,"
                " %s, 'pending_confirmation', %s, %s, %s, %s, %s, %s)",
                (
                    f"{x.hazard.replace('_', ' ')} in {x.formation or 'unknown formation'}{depth}",
                    x.what_happened,
                    x.what_worked,
                    x.hazard,
                    x.formation_id,
                    f"pipeline:{version}",
                    audio["block_id"],
                    audio.get("jv_id"),
                    src,
                    x.md_top,
                    x.md_base,
                ),
            )
            counts["lessons"] += 1
    return counts


def formation_names(conn: Any) -> dict[str, int]:
    """lower-case name and every synonym -> formation id."""
    out: dict[str, int] = {}
    for fid, name, syns in conn.execute("SELECT id, name, synonyms FROM formation"):
        for n in [name, *(syns or [])]:
            out.setdefault(n.lower(), fid)
    return out
