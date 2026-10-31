"""Role-based provider router (docs/04).

Maps an engine role (narrator, dialogue, planner, …) to a bound provider. One
physical model may serve every role in Phase 0; the indirection costs nothing and
lets per-role optimization arrive later without touching the pipeline.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from uro_core.metering import RESPONSE_CAP, LLMCall, prompt_hash
from uro_core.providers.base import CompletionRequest, LLMProvider, Message, Usage, collect_usage

logger = logging.getLogger(__name__)

# Where a router records each call (D-66) — the Engine points it at its store's `record_llm_call`.
Recorder = Callable[[LLMCall], Awaitable[None]]


class ProviderRouter:
    def __init__(
        self,
        bindings: dict[str, LLMProvider],
        default: LLMProvider | None = None,
        *,
        recorder: Recorder | None = None,
    ) -> None:
        self._bindings = bindings
        self._default = default
        # The router's own recorder meters calls made without one (D-66): the world-authoring stages
        # (backfill, probe). An Engine passes ITS store's recorder on every call, so two engines
        # sharing one router each meter into their own store (review).
        self.recorder = recorder

    def _provider_for(self, role: str) -> LLMProvider:
        provider = self._bindings.get(role, self._default)
        if provider is None:
            raise KeyError(f"no provider bound for role {role!r} and no default set")
        return provider

    def has_role(self, role: str) -> bool:
        """True if `role` is EXPLICITLY bound (not merely resolvable via the default). Used to make
        an optional role (e.g. `dialogue` speech-routing) opt-in: bound → on, unbound → off."""
        return role in self._bindings

    def model_for(self, role: str) -> str | None:
        """The model id serving `role`, for metering — or None if nothing serves it or the provider
        doesn't expose a `model` (the port doesn't require one; the stub has none)."""
        model = getattr(self._bindings.get(role, self._default), "model", None)
        return model if isinstance(model, str) and model else None

    async def stream(
        self,
        role: str,
        messages: list[Message],
        *,
        temperature: float = 0.9,
        max_tokens: int | None = None,
        branch_id: str | None = None,
        recorder: Recorder | None = None,
    ) -> AsyncIterator[str]:
        provider = self._provider_for(role)
        req = CompletionRequest(
            messages=messages,
            stage_tag=role,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        started = time.perf_counter()
        with collect_usage() as usage:
            try:
                async for chunk in provider.stream(req):
                    yield chunk
            finally:  # narration is the beat's own text: metered, not stored again here
                await self._record(role, messages, started, usage, None, branch_id, recorder)

    async def complete(
        self,
        role: str,
        messages: list[Message],
        *,
        json_mode: bool = False,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        branch_id: str | None = None,
        recorder: Recorder | None = None,
    ) -> str:
        provider = self._provider_for(role)
        req = CompletionRequest(
            messages=messages,
            stage_tag=role,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
        started = time.perf_counter()
        raw: str | None = None
        with collect_usage() as usage:
            try:
                raw = await provider.complete(req)
                return raw
            except Exception as exc:
                # a failure keeps whatever the provider did return — a truncated extractor JSON is
                # exactly the response worth reading afterwards (review)
                raw = getattr(exc, "partial_response", None)
                raise
            finally:  # a structured call: its full response is kept
                await self._record(role, messages, started, usage, raw, branch_id, recorder)

    async def embed(
        self,
        role: str,
        texts: list[str],
        *,
        branch_id: str | None = None,
        recorder: Recorder | None = None,
    ) -> list[list[float]]:
        provider = self._provider_for(role)
        started = time.perf_counter()
        with collect_usage() as usage:
            try:
                return await provider.embed(texts)
            finally:
                messages = [Message(role="user", content=t) for t in texts]
                await self._record(role, messages, started, usage, None, branch_id, recorder)

    async def _record(
        self,
        role: str,
        messages: list[Message],
        started: float,
        usage: Usage,
        response: str | None,
        branch_id: str | None,
        recorder: Recorder | None = None,
    ) -> None:
        """Meter one call into the caller's recorder, else the router's. Best-effort observability:
        it must never break the call it measures."""
        record = recorder or self.recorder
        if record is None:
            return
        try:
            await record(
                LLMCall(
                    stage_tag=role,
                    prompt_hash=prompt_hash(messages),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    model=self.model_for(role),
                    tokens_in=usage.tokens_in,
                    tokens_out=usage.tokens_out,
                    branch_id=branch_id,
                    response=response[:RESPONSE_CAP] if response is not None else None,
                )
            )
        except Exception as exc:
            logger.warning("metering failed for stage %s: %s", role, exc)
