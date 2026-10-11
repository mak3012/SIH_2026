"""The pipelines' prompts and the shared LLM client (core.llm) bound to them."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from core import llm as _core
from core.llm import CallLog, JsonlLog, LLMConfig, PgLog, Prompt, strict

PROMPTS_DIR = Path(__file__).with_name("prompts")
# Pinned prompt versions. A new version is a new file; old files stay so logs stay explainable.
PROMPT_VERSIONS = {
    "extract_wcr": "v2",
    "extract_ddr": "v2",
    "extract_survey_report": "v2",
    "extract_field_note": "v2",
    "transcribe_page": "v2",
    "extract_lessons": "v2",
}
PROMPTS = _core.PromptSet(PROMPTS_DIR, PROMPT_VERSIONS)


def prompt(name: str, version: str | None = None) -> Prompt:
    return PROMPTS.get(name, version)


class LLM(_core.LLM):
    def __init__(
        self,
        cfg: LLMConfig,
        log: CallLog,
        transport: Callable[[str, dict[str, Any], int], dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(cfg, log, transport, PROMPTS)


__all__ = [
    "LLM",
    "PROMPT_VERSIONS",
    "CallLog",
    "JsonlLog",
    "LLMConfig",
    "PgLog",
    "prompt",
    "strict",
]
