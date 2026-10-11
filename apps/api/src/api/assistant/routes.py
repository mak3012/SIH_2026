"""Assistant endpoints.

    POST   /assistant/ask                       {"question", "conversation_id"?}
    POST   /assistant/ask/stream                same body; server-sent events (see ask_stream)
    POST   /assistant/ask/voice?conversation_id=&language=   raw audio body (wav, mp3, ogg, m4a)
    GET    /assistant/conversations
    GET    /assistant/conversations/{id}
    DELETE /assistant/conversations/{id}

Any role may ask (answers come only from rows the caller's licences allow). Conversations are
private to their user.
"""

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator
from functools import cache
from typing import Any

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.assistant import memory, voice
from api.assistant.retrieval import embedder_from_env
from api.assistant.service import PROMPTS, Assistant
from api.auth import CurrentUser, User
from core.llm import LLM, LLMConfig, PgLog

router = APIRouter(prefix="/assistant", tags=["assistant"])
MAX_AUDIO = 10 * 1024 * 1024


def _connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"])


@cache
def assistant() -> Assistant:
    """ASSISTANT_DRAFTER=rules: answers are drafted by the rule composer only, while the LLM
    still explains alerts (a laptop CPU is too slow for both; answers are verified either way)."""
    if os.environ.get("ASSISTANT_DRAFTER", "llm") == "rules":
        return Assistant(_connect, None, embedder_from_env())
    llm = LLM(
        LLMConfig.from_env(),
        PgLog(lambda: psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)),
        prompts=PROMPTS,
    )
    return Assistant(_connect, llm, embedder_from_env())


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: int | None = None


def _answer(user: User, question: str, conversation_id: int | None, mode: str) -> dict[str, Any]:
    try:
        previous, history = memory.context(user, conversation_id)
    except LookupError as e:
        raise HTTPException(404, "conversation not found") from e
    res = assistant().ask(user, question, previous, history)
    conv, turn = memory.save(user, conversation_id, question, res, mode)
    return {"conversation_id": conv, "turn_id": turn, "question": question, **res.json()}


@router.post("/ask")
def ask(req: AskRequest, user: User = CurrentUser) -> dict[str, Any]:
    """Answer from the records, with citations and the evidence behind them."""
    return _answer(user, req.question, req.conversation_id, "text")


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.post("/ask/stream")
async def ask_stream(req: AskRequest, user: User = CurrentUser) -> StreamingResponse:
    """The same answer as POST /ask, as server-sent events:

    status  {"stage": "answering"}            at once, so the client can show progress
    delta   {"text": "..."}                   the answer, a few words at a time
    done    {<the POST /ask response>}        citations, evidence, follow-ups, status
    error   {"status": 404, "detail": "..."}  instead of done

    The answer is drafted and every number verified before the first delta: streaming never
    shows a number the verifier would have removed."""

    async def events() -> AsyncIterator[str]:
        yield _sse("status", {"stage": "answering"})
        try:
            out = await asyncio.to_thread(_answer, user, req.question, req.conversation_id, "text")
        except HTTPException as e:
            yield _sse("error", {"status": e.status_code, "detail": e.detail})
            return
        for piece in re.findall(r"\S+\s*", out.get("answer") or ""):
            yield _sse("delta", {"text": piece})
            await asyncio.sleep(0.012)
        yield _sse("done", out)

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


@router.post("/ask/voice")
async def ask_voice(
    request: Request,
    user: User = CurrentUser,
    conversation_id: int | None = None,
    language: str | None = Query(None, pattern="^(en|hi|as)$"),
) -> dict[str, Any]:
    """Spoken question (raw audio body): transcribed with faster-whisper, then answered."""
    if not voice.available():
        raise HTTPException(503, "speech recognition is not installed (extra 'speech')")
    audio = await request.body()
    if not audio:
        raise HTTPException(422, "empty audio")
    if len(audio) > MAX_AUDIO:
        raise HTTPException(413, "audio larger than 10 MB")
    t = await asyncio.to_thread(voice.transcribe, audio, language)
    if not t.text:
        raise HTTPException(422, "no speech recognised")
    out = await asyncio.to_thread(_answer, user, t.text, conversation_id, "voice")
    return {
        "transcript": {
            "text": t.text,
            "language": t.language,
            "probability": round(t.probability, 3),
        },
        **out,
    }


@router.get("/conversations")
def list_conversations(user: User = CurrentUser) -> list[dict[str, Any]]:
    return memory.conversations(user)


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: int, user: User = CurrentUser) -> list[dict[str, Any]]:
    turns = memory.turns(user, conversation_id)
    if not turns:
        raise HTTPException(404, "conversation not found")
    return turns


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: int, user: User = CurrentUser) -> dict[str, Any]:
    if not memory.forget(user, conversation_id):
        raise HTTPException(404, "conversation not found")
    return {"id": conversation_id, "deleted": True}
