"""Codex (ChatGPT-subscription) inference adapter — the OpenAI **Responses API** (D-47).

Consumes the OAuth tokens minted by `providers/codex_auth.py` and speaks the Responses API at the
ChatGPT backend (`.../codex/responses`), NOT Chat Completions. It translates uro's
Chat-Completions-shaped `CompletionRequest` (a message list) into Responses' `instructions` +
`input`-item shape, and parses the Responses SSE event stream back to text.

Store-agnostic: it takes a `token_provider(force_refresh) -> access_token` async callable, so the
same adapter serves both the pure one-shot probe (a static token) and the router (a refresh-capable
token source). A 401 triggers one forced-refresh retry.

Reasoning-model note: the Responses body carries NO temperature (gpt-5/codex reject a non-default
one); `max_tokens` maps to `max_output_tokens`. Embeddings are unsupported (no backend endpoint).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx

from uro_core.errors import ProviderError
from uro_core.providers.base import CompletionRequest
from uro_core.providers.codex_auth import CODEX_BASE_URL, codex_inference_headers

# force_refresh -> a valid access token (refreshed+persisted by the caller if it wired that in).
TokenProvider = Callable[[bool], Awaitable[str]]


class CodexResponsesProvider:
    def __init__(
        self,
        *,
        model: str,
        token_provider: TokenProvider,
        base_url: str = CODEX_BASE_URL,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._token_provider = token_provider
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport  # test seam; None = real network transport

    def _body(self, req: CompletionRequest) -> dict[str, object]:
        # System messages become the Responses `instructions`; user/assistant turns become typed
        # input items (`input_text` vs `output_text`).
        instructions = "\n\n".join(m.content for m in req.messages if m.role == "system")
        input_items: list[dict[str, object]] = []
        for m in req.messages:
            if m.role == "system":
                continue
            kind = "output_text" if m.role == "assistant" else "input_text"
            input_items.append({"role": m.role, "content": [{"type": kind, "text": m.content}]})
        body: dict[str, object] = {
            "model": self._model,
            "input": input_items,
            "stream": True,
            "store": False,  # no server-side conversation persistence
        }
        if instructions:
            body["instructions"] = instructions
        if req.max_tokens is not None:
            body["max_output_tokens"] = req.max_tokens
        if req.json_mode:
            # Responses structured-output request. UNVERIFIED against the codex backend — the
            # planner/extractor prompts also instruct JSON, so this is belt-and-suspenders.
            body["text"] = {"format": {"type": "json_object"}}
        return body

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        body = self._body(req)
        url = f"{self._base_url}/responses"
        # Attempt 0: current token. Attempt 1: force a refresh (the 401-retry path). A 401 on the
        # FINAL attempt reaches raise_for_status → the HTTPStatusError handler renders it as a clean
        # "reconnect" (the token is dead, not merely stale) rather than a raw httpx message.
        for attempt in range(2):
            token = await self._token_provider(attempt == 1)
            headers = codex_inference_headers(token)
            try:
                async with (
                    httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client,
                    client.stream("POST", url, headers=headers, json=body) as resp,
                ):
                    if resp.status_code == 401 and attempt == 0:
                        continue  # token likely stale → refresh and retry once
                    resp.raise_for_status()
                    async for piece in self._parse_sse(resp):
                        yield piece
                    return
            except httpx.HTTPStatusError as exc:
                detail = (
                    "401 Unauthorized after refresh — reconnect"
                    if exc.response.status_code == 401
                    else f"HTTP {exc.response.status_code}"
                )
                raise ProviderError(f"codex request failed: {detail}") from exc
            except httpx.HTTPError as exc:
                raise ProviderError(f"codex request failed: {exc}") from exc

    async def complete(self, req: CompletionRequest) -> str:
        # The Responses path always streams; the extractor/planner just want the whole text.
        return "".join([piece async for piece in self.stream(req)])

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise ProviderError(
            "the codex (ChatGPT-subscription) provider has no embedding endpoint; bind the "
            "embedder role to an API-key provider (openai/local)"
        )

    async def _parse_sse(self, resp: httpx.Response) -> AsyncIterator[str]:
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if not data or data == "[DONE]":
                continue
            try:
                evt = json.loads(data)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type")
            if etype == "response.output_text.delta":
                piece = evt.get("delta")
                if piece:
                    yield piece
            elif etype in ("response.failed", "error"):
                err = evt.get("error") or (evt.get("response") or {}).get("error") or {}
                msg = err.get("message") if isinstance(err, dict) else str(err)
                raise ProviderError(f"codex responded with an error: {msg}")
