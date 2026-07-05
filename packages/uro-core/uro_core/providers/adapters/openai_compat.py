"""OpenAI-compatible streaming provider (docs/04).

Covers OpenAI, Ollama's /v1 endpoint, vLLM, and most gateways with one adapter.
Streams via SSE `chat/completions`. API key optional (local endpoints need none).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from uro_core.providers.base import CompletionRequest


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

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

        async with (
            httpx.AsyncClient(timeout=self._timeout) as client,
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
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    yield piece
