"""LLM provider PORT (docs/04).

Phase 0 needs only streamed text completion. Structured output and embeddings
arrive in Phase 1; usage metering is captured from Phase 0 by the engine timing
each call (docs/01 rule 5, D-14 — token counts join when this port grows a usage
channel). Concrete adapters live in providers/adapters/ and are banned from the
core ring (D-27).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal, Protocol

from pydantic import BaseModel

Role = Literal["system", "user", "assistant"]


class Message(BaseModel):
    role: Role
    content: str


class CompletionRequest(BaseModel):
    messages: list[Message]
    stage_tag: str  # engine role (narrator, dialogue, …) — for metering (docs/04)
    temperature: float = 0.9
    max_tokens: int | None = None
    json_mode: bool = False  # request a JSON object response (planner/extractor, docs/13)


class LLMProvider(Protocol):
    def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        """Yield text chunks as they arrive.

        Declared as a plain `def` returning an async iterator — that is the type of
        an async-generator method, which is how adapters implement it.
        """
        ...

    async def complete(self, req: CompletionRequest) -> str:
        """Return a full (non-streamed) completion — used by the extractor/planner."""
        ...

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text (semantic recall, docs/04)."""
        ...
