"""LLM provider PORT (docs/04).

Phase 0 needs only streamed text completion. Structured output and embeddings
arrive in Phase 1; usage metering is captured from Phase 0 by the engine timing
each call (docs/01 rule 5, D-14 — token counts join when this port grows a usage
channel). Concrete adapters live in providers/adapters/ and are banned from the
core ring (D-27).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
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


# --- usage reporting (D-66) -------------------------------------------------------------------
# The provider port returns text, so token counts travel on a side channel: the router opens a
# collector around each call, and an adapter that can read the provider's usage reports it. It is
# OPTIONAL — a provider that doesn't report is still metered, without tokens — so a custom provider
# keeps working unchanged. A ContextVar keeps concurrent calls (several beats at once) apart.


@dataclass
class Usage:
    tokens_in: int | None = None
    tokens_out: int | None = None


_USAGE: ContextVar[Usage | None] = ContextVar("uro_llm_usage", default=None)


def report_usage(*, tokens_in: int | None = None, tokens_out: int | None = None) -> None:
    """Report the token counts the provider returned for the call in progress. Counts add up, so
    an adapter that makes several requests for one call reports each one's usage. A no-op outside
    a metered call."""
    usage = _USAGE.get()
    if usage is None:
        return
    if tokens_in is not None:
        usage.tokens_in = (usage.tokens_in or 0) + int(tokens_in)
    if tokens_out is not None:
        usage.tokens_out = (usage.tokens_out or 0) + int(tokens_out)


@contextmanager
def collect_usage() -> Iterator[Usage]:
    """Collect what adapters report during the enclosed call (the router's side of the channel)."""
    usage = Usage()
    token = _USAGE.set(usage)
    try:
        yield usage
    finally:
        with suppress(ValueError):  # a stream finalized from another context: nothing to restore
            _USAGE.reset(token)
