"""Role-based provider router (docs/04).

Maps an engine role (narrator, dialogue, planner, …) to a bound provider. One
physical model may serve every role in Phase 0; the indirection costs nothing and
lets per-role optimization arrive later without touching the pipeline.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from uro_core.providers.base import CompletionRequest, LLMProvider, Message


class ProviderRouter:
    def __init__(
        self, bindings: dict[str, LLMProvider], default: LLMProvider | None = None
    ) -> None:
        self._bindings = bindings
        self._default = default

    def _provider_for(self, role: str) -> LLMProvider:
        provider = self._bindings.get(role, self._default)
        if provider is None:
            raise KeyError(f"no provider bound for role {role!r} and no default set")
        return provider

    async def stream(
        self,
        role: str,
        messages: list[Message],
        *,
        temperature: float = 0.9,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        provider = self._provider_for(role)
        req = CompletionRequest(
            messages=messages,
            stage_tag=role,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        async for chunk in provider.stream(req):
            yield chunk

    async def complete(
        self,
        role: str,
        messages: list[Message],
        *,
        json_mode: bool = False,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> str:
        provider = self._provider_for(role)
        req = CompletionRequest(
            messages=messages,
            stage_tag=role,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
        return await provider.complete(req)

    async def embed(self, role: str, texts: list[str]) -> list[list[float]]:
        return await self._provider_for(role).embed(texts)
