import json
from typing import Any

from pipelines.llm import LLM, LLMConfig, prompt, strict
from pipelines.schemas import WCR


class Recorder:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def log(self, record: dict[str, Any]) -> None:
        self.records.append(record)


def test_retry_with_strict_schema_and_logging() -> None:
    bodies: list[dict[str, Any]] = []
    replies = iter(['{"well_name": 5}', json.dumps({"well_name": "SA-01", "td_md": "9,851 ft"})])

    def transport(url: str, body: dict[str, Any], timeout: int) -> dict[str, Any]:
        bodies.append(body)
        return {"message": {"content": next(replies)}, "model": body["model"]}

    log = Recorder()
    llm = LLM(LLMConfig(backend="ollama", url="http://x"), log, transport)
    llm._versions["qwen2.5:7b-instruct"] = "qwen2.5:7b-instruct@abc123"
    out = llm.complete_json(
        "extract_wcr",
        {"pages": "[page 1]\nWell name | SA-01"},
        WCR.model_json_schema(by_alias=True),
        validate=WCR.model_validate,
        document_id=7,
    )
    assert out.well_name == "SA-01" and out.td_md == "9,851 ft"
    assert [b["options"]["temperature"] for b in bodies] == [0, 0]
    assert bodies[1]["format"]["additionalProperties"] is False  # stricter schema on retry
    assert "did not validate" in bodies[1]["messages"][0]["content"]
    first, second = log.records
    assert (first["attempt"], first["parsed_ok"], first["strict_schema"]) == (1, False, False)
    assert (second["attempt"], second["parsed_ok"], second["strict_schema"]) == (2, True, True)
    assert second["prompt_id"] == "extract_wcr@v2" and len(second["input_hash"]) == 64
    assert second["model_version"] == "qwen2.5:7b-instruct@abc123"
    assert second["document_id"] == 7 and first["input_hash"] != second["input_hash"]


def test_vllm_request_uses_json_schema_response_format() -> None:
    bodies: list[dict[str, Any]] = []

    def transport(url: str, body: dict[str, Any], timeout: int) -> dict[str, Any]:
        bodies.append(body)
        assert url.endswith("/v1/chat/completions")
        return {
            "model": "m",
            "system_fingerprint": "fp1",
            "choices": [{"message": {"content": '{"well_name": "X"}'}}],
        }

    log = Recorder()
    llm = LLM(LLMConfig(backend="vllm", url="http://v", model="m"), log, transport)
    llm.complete_json("extract_survey_report", {"pages": ""}, {"type": "object"})
    assert bodies[0]["response_format"]["type"] == "json_schema" and bodies[0]["temperature"] == 0
    assert log.records[0]["model_version"] == "m#fp1"


def test_disabled_backend_returns_none_and_logs_nothing() -> None:
    log = Recorder()
    llm = LLM(LLMConfig(backend="none"), log)
    assert llm.complete_json("extract_ddr", {"pages": ""}, {}) is None
    assert log.records == []


def test_prompts_are_versioned_files() -> None:
    p = prompt("extract_lessons")
    assert p.id == "extract_lessons@v2" and "$transcript" in p.text and len(p.sha256) == 64


def test_strict_requires_every_property() -> None:
    s = strict(WCR.model_json_schema(by_alias=True))
    assert set(s["required"]) == set(s["properties"])
    assert s["$defs"]["TopRow"]["additionalProperties"] is False
