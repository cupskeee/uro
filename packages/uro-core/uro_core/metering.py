"""Usage metering record (docs/01 rule 5, docs/07, D-14).

Stage-tagged LLM usage, captured from Phase 0 — the docs mandate this from the
start precisely because retrofitting observability is misery. Every call through
the `ProviderRouter` is recorded (D-66): stage, model, latency, prompt hash, the
token counts its adapter reported, the world/branch it served, and — for a
structured (non-streamed) call — the full response. This data is operational and
prunable — never world truth, never on the timeline.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from uro_core.domain.ids import new_id

if TYPE_CHECKING:
    from uro_core.providers.base import Message

# A stored response is capped — a runaway one shouldn't bloat the table (extractor output with
# max_tokens 4096 is a few thousand characters).
RESPONSE_CAP = 64_000


def describe_failure(exc: BaseException) -> str:
    """Why a call failed, safe to store: the exception's type and its causes' types, plus an HTTP
    status where one is known — e.g. `ProviderError ← HTTPStatusError (HTTP 429)`. Never the
    exception TEXT: an httpx header error can embed the Authorization value (D-47 review)."""
    parts: list[str] = []
    link: BaseException | None = exc
    while link is not None and len(parts) < 3:
        status = getattr(getattr(link, "response", None), "status_code", None)
        parts.append(type(link).__name__ + (f" (HTTP {status})" if status else ""))
        link = link.__cause__ or (None if link.__suppress_context__ else link.__context__)
    return " ← ".join(parts)


def prompt_hash(messages: list[Message]) -> str:
    payload = json.dumps([m.model_dump() for m in messages], sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LLMCall(BaseModel):
    call_id: str = Field(default_factory=new_id)
    stage_tag: str  # engine role: narrator, dialogue, planner, …
    prompt_hash: str
    latency_ms: int
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    branch_id: str | None = None  # the branch the call served (its world is looked up), if any
    response: str | None = None  # a structured call's full response, capped at RESPONSE_CAP
    error: str | None = None  # why a failed call failed (`describe_failure`); None = it succeeded
