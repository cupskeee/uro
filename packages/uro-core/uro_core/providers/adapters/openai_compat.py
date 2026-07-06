"""OpenAI-compatible streaming provider (docs/04).

Covers OpenAI, Ollama's /v1 endpoint, vLLM, and most gateways with one adapter.
Streams via SSE `chat/completions`. API key optional (local endpoints need none).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from uro_core.errors import ProviderError
from uro_core.providers.base import CompletionRequest


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport  # test seam; None = real network transport

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body: dict[str, object] = {
            "model": self._model,
            "messages": [m.model_dump() for m in req.messages],
            "temperature": req.temperature,
            "stream": True,
        }
        if req.max_tokens is not None:
            body["max_tokens"] = req.max_tokens

        try:
            async with (
                httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client,
                client.stream(
                    "POST", f"{self._base_url}/chat/completions", headers=headers, json=body
                ) as resp,
            ):
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    # In-band error frame (HTTP 200, then `data: {"error": ...}`):
                    # abort loudly instead of ending the stream as if it succeeded.
                    err = chunk.get("error")
                    if err is not None:
                        msg = err.get("message") if isinstance(err, dict) else str(err)
                        raise ProviderError(f"provider returned an in-band error: {msg}")
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield piece
        except httpx.HTTPError as exc:
            raise ProviderError(f"provider request failed: {exc}") from exc

    async def complete(self, req: CompletionRequest) -> str:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body: dict[str, object] = {
            "model": self._model,
            "messages": [m.model_dump() for m in req.messages],
            "temperature": req.temperature,
            "stream": False,
        }
        if req.json_mode:
            body["response_format"] = {"type": "json_object"}
        if req.max_tokens is not None:
            body["max_tokens"] = req.max_tokens

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(
                    f"{self._base_url}/chat/completions", headers=headers, json=body
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise ProviderError(f"provider request failed: {exc}") from exc
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("provider returned no choices")
        return choices[0].get("message", {}).get("content") or ""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = {"model": self._model, "input": texts}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(f"{self._base_url}/embeddings", headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise ProviderError(f"embedding request failed: {exc}") from exc
        rows = data.get("data") or []
        return [row["embedding"] for row in rows]
