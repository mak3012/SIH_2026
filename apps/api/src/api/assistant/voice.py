"""Spoken questions: faster-whisper transcription (optional extra `speech`).

    uv sync --package api --extra speech

Model WHISPER_MODEL (default `small`, CPU int8). Language: as given, else detected among
English, Hindi and Assamese. The transcript is what gets answered and is returned with the
answer so the user can see what was heard.
"""

import os
import tempfile
from dataclasses import dataclass
from functools import cache
from typing import Any

LANGS = ("en", "hi", "as")


@dataclass
class Transcript:
    text: str
    language: str
    probability: float
    duration_s: float


@cache
def _model() -> Any:
    from faster_whisper import WhisperModel

    return WhisperModel(os.environ.get("WHISPER_MODEL", "small"), device="cpu", compute_type="int8")


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def transcribe(audio: bytes, language: str | None = None) -> Transcript:
    if language is not None and language not in LANGS:
        raise ValueError(f"language must be one of {LANGS}")
    with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as f:
        f.write(audio)
        path = f.name
    try:
        segments, info = _model().transcribe(path, language=language, vad_filter=True, beam_size=5)
        text = " ".join(s.text.strip() for s in segments).strip()
    finally:
        os.unlink(path)
    return Transcript(text, info.language, float(info.language_probability), float(info.duration))
