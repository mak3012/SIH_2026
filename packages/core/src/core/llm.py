"""LLM / VLM calls shared by the pipelines and the API: versioned prompt files, guided JSON,
temperature 0, a log entry per attempt, stricter-schema retries.

Backends (LLM_BACKEND): `ollama` (native /api/chat with `format: <JSON schema>`), `vllm`
(OpenAI-compatible /v1/chat/completions with `response_format: json_schema`, which vLLM
enforces with guided decoding), or `none`. Every attempt is logged with the input hash, the
raw output and the model version. If the output does not validate, the call is retried with a
stricter schema (every property required, no extra keys) and the validation error appended.

Each package keeps its own prompt files and pins their versions in a `PromptSet`.
"""

import copy
import hashlib
import json
import os
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from string import Template
from typing import Any, Protocol

from core.telemetry import LLM_LATENCY, tracer


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str

    @property
    def id(self) -> str:
        return f"{self.name}@{self.version}"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()

    def render(self, variables: dict[str, Any]) -> str:
        return Template(self.text).substitute(variables)


class PromptSet:
    """Prompt files `<root>/<name>/<version>.md`, with pinned versions. A new version is a new
    file and a new pin; old files stay so logged calls stay explainable."""

    def __init__(self, root: Path, versions: dict[str, str]) -> None:
        self.root, self.versions = root, versions

    def get(self, name: str, version: str | None = None) -> Prompt:
        return _load(self.root, name, version or self.versions[name])


@cache
def _load(root: Path, name: str, version: str) -> Prompt:
    return Prompt(name, version, (root / name / f"{version}.md").read_text(encoding="utf-8"))


class CallLog(Protocol):
    def log(self, record: dict[str, Any]) -> None: ...


class JsonlLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, record: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")


class PgLog:
    """Writes to the llm_call table (migrations 0009, 0012)."""

    def __init__(self, connect: Callable[[], Any]) -> None:
        self.connect = connect

    def log(self, record: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        with self.connect() as conn:
            conn.execute(
                "INSERT INTO llm_call (backend, model, model_version, prompt_id, prompt_sha256,"
                " temperature, input_hash, attempt, strict_schema, request, output, parsed_ok,"
                " error, latency_ms, document_id, audio_note_id, alert_id) VALUES (%s, %s, %s,"
                " %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record["backend"],
                    record["model"],
                    record["model_version"],
                    record["prompt_id"],
                    record["prompt_sha256"],
                    record["temperature"],
                    record["input_hash"],
                    record["attempt"],
                    record["strict_schema"],
                    Jsonb(record["request"]),
                    record["output"],
                    record["parsed_ok"],
                    record["error"],
                    record["latency_ms"],
                    record.get("document_id"),
                    record.get("audio_note_id"),
                    record.get("alert_id"),
                ),
            )


def strict(schema: dict[str, Any]) -> dict[str, Any]:
    """Every object property required (nullable ones may be null) and no extra keys."""
    s = copy.deepcopy(schema)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(s)
    return s


@dataclass
class LLMConfig:
    backend: str = "none"  # none | ollama | vllm
    url: str = "http://localhost:11434"
    model: str = "qwen2.5:7b-instruct"
    vision_model: str = "qwen2.5vl:7b"
    timeout_s: int = 300
    max_attempts: int = 3
    # Output cap per call: a small model on a CPU can otherwise generate until the timeout.
    max_tokens: int = 2048

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            backend=os.environ.get("LLM_BACKEND", "none"),
            url=os.environ.get("LLM_URL", os.environ.get("OLLAMA_URL", cls.url)),
            model=os.environ.get("LLM_MODEL", cls.model),
            vision_model=os.environ.get("VLM_MODEL", cls.vision_model),
            timeout_s=int(os.environ.get("LLM_TIMEOUT_S", cls.timeout_s)),
            max_tokens=int(os.environ.get("LLM_MAX_TOKENS", cls.max_tokens)),
        )


class LLM:
    def __init__(
        self,
        cfg: LLMConfig,
        log: CallLog,
        transport: Callable[[str, dict[str, Any], int], dict[str, Any]] | None = None,
        prompts: PromptSet | None = None,
    ) -> None:
        self.cfg = cfg
        self.prompts = prompts
        self.calls = log
        self.post = transport or _post
        self._versions: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return self.cfg.backend in ("ollama", "vllm")

    def complete_json(
        self,
        prompt_name: str,
        variables: dict[str, Any],
        schema: dict[str, Any],
        *,
        images: list[str] | None = None,
        vision: bool = False,
        validate: Callable[[Any], Any] | None = None,
        **log_ctx: Any,
    ) -> Any:
        """JSON matching `schema`, validated by `validate` (which raises on bad output), or
        None when every attempt failed or no backend is configured."""
        if not self.enabled:
            return None
        if self.prompts is None:
            raise ValueError("LLM has no PromptSet")
        p = self.prompts.get(prompt_name)
        model = self.cfg.vision_model if vision else self.cfg.model
        text = p.render(variables)
        feedback = ""
        for attempt in range(1, self.cfg.max_attempts + 1):
            use_strict = attempt > 1
            sch = strict(schema) if use_strict else schema
            content = text + (f"\n\n{feedback}" if feedback else "")
            body, url = self._request(model, content, sch, images)
            input_hash = hashlib.sha256(
                json.dumps(
                    [
                        self.cfg.backend,
                        model,
                        p.id,
                        content,
                        sch,
                        [hashlib.sha256(i.encode()).hexdigest() for i in images or []],
                    ],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            t0 = time.monotonic()
            output, error, parsed, result = None, None, False, None
            attrs: dict[str, str | int] = {
                "llm.prompt": p.id,
                "llm.model": model,
                "llm.attempt": attempt,
            }
            with tracer.start_as_current_span("llm.call", attributes=attrs) as span:
                try:
                    resp = self.post(url, body, self.cfg.timeout_s)
                    output = self._content(resp)
                    result = json.loads(output)
                    if validate is not None:
                        result = validate(result)
                    parsed = True
                except Exception as e:  # logged, then retried with the stricter schema
                    error = f"{type(e).__name__}: {e}"
                    feedback = (
                        "Your previous answer did not validate: "
                        f"{error[:800]}\nReturn only JSON that matches the schema exactly."
                    )
                outcome = "ok" if parsed else ("invalid" if output is not None else "error")
                span.set_attribute("llm.outcome", outcome)
            LLM_LATENCY.labels(prompt_name, model, outcome).observe(time.monotonic() - t0)
            logged_body = {k: v for k, v in body.items() if k != "messages"}
            logged_body["messages"] = [
                {"role": "user", "content": content, "images": len(images or [])}
            ]
            self.calls.log(
                {
                    "backend": self.cfg.backend,
                    "model": model,
                    "model_version": self.model_version(model),
                    "prompt_id": p.id,
                    "prompt_sha256": p.sha256,
                    "temperature": 0.0,
                    "input_hash": input_hash,
                    "attempt": attempt,
                    "strict_schema": use_strict,
                    "request": logged_body,
                    "output": output,
                    "parsed_ok": parsed,
                    "error": error,
                    "latency_ms": int((time.monotonic() - t0) * 1000),
                    **log_ctx,
                }
            )
            if parsed:
                return result
        return None

    def _request(
        self, model: str, content: str, schema: dict[str, Any], images: list[str] | None
    ) -> tuple[dict[str, Any], str]:
        if self.cfg.backend == "ollama":
            msg: dict[str, Any] = {"role": "user", "content": content}
            if images:
                msg["images"] = images
            return (
                {
                    "model": model,
                    "messages": [msg],
                    "format": schema,
                    "stream": False,
                    "options": {"temperature": 0, "seed": 0, "num_predict": self.cfg.max_tokens},
                },
                f"{self.cfg.url.rstrip('/')}/api/chat",
            )
        parts: list[dict[str, Any]] = [{"type": "text", "text": content}]
        parts += [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{i}"}}
            for i in images or []
        ]
        return (
            {
                "model": model,
                "messages": [{"role": "user", "content": parts}],
                "temperature": 0,
                "seed": 0,
                "max_tokens": self.cfg.max_tokens,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "output", "schema": schema, "strict": True},
                },
            },
            f"{self.cfg.url.rstrip('/')}/v1/chat/completions",
        )

    def _content(self, resp: dict[str, Any]) -> str:
        if self.cfg.backend == "ollama":
            return str(resp["message"]["content"])
        if fp := resp.get("system_fingerprint"):
            self._versions.setdefault(str(resp.get("model")), f"{resp.get('model')}#{fp}")
        return str(resp["choices"][0]["message"]["content"])

    def model_version(self, model: str) -> str:
        """Ollama: the model digest from /api/tags. vLLM: served model id (+ fingerprint)."""
        if model in self._versions:
            return self._versions[model]
        version = model
        if self.cfg.backend == "ollama":
            try:
                with urllib.request.urlopen(
                    f"{self.cfg.url.rstrip('/')}/api/tags", timeout=10
                ) as r:
                    tags = json.load(r).get("models", [])
                version = next(
                    (f"{m['name']}@{m['digest'][:12]}" for m in tags if m.get("name") == model),
                    model,
                )
            except OSError:
                pass
        self._versions[model] = version
        return version


def _post(url: str, body: dict[str, Any], timeout: int) -> dict[str, Any]:
    req = urllib.request.Request(
        url, json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return dict(json.load(r))
