"""OpenAI-compatible streaming provider (docs/04).

Covers OpenAI, Ollama's /v1 endpoint, vLLM, and most gateways with one adapter.
Streams via SSE `chat/completions`. API key optional (local endpoints need none).
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator

import httpx

from uro_core.errors import ProviderError
from uro_core.providers.base import CompletionRequest

# OpenAI o-series reasoning models (o1 / o3 / o3-mini / o4-mini / …) speak a slightly different
# Chat Completions dialect: they REJECT `max_tokens` (require `max_completion_tokens` instead) and
# accept ONLY the default `temperature` (1) — sending either legacy field is a hard 400, so a plain
# `test` probe or beat fails on a perfectly valid key. Detected by the `o<digit>` naming convention
# (these ship on OpenAI + compatible proxies that emulate them). gpt-5 reasoning variants may share
# this contract but their chat variants share the name, so they're NOT matched here (unverified).
# We remediate only the temperature + token-field axis; the OLDEST o1-preview/o1-mini additionally
# reject the `system` role and json-mode `response_format` — that axis is an unhandled deferral
# (docs/CHANGELOG), so an o1-mini binding on the extractor/planner (json + system prompt) can still
# 400. The current common reasoning models (o3-mini, o4-mini) accept both.
_REASONING_MODEL_RE = re.compile(r"^o\d")


def _is_reasoning_model(model: str) -> bool:
    # Test the FINAL path segment so a gateway-namespaced id (OpenRouter `openai/o4-mini`, an Azure
    # `foo/o3-mini`, …) is still recognised — the anchored regex would otherwise miss exactly the
    # "compatible proxy" surface this adapter targets, sending the legacy fields → the same 400.
    return bool(_REASONING_MODEL_RE.match(model.lower().rsplit("/", 1)[-1]))


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

    def _apply_sampling(self, body: dict[str, object], req: CompletionRequest) -> None:
        """Add the temperature + token-cap fields to `body`, dialect-aware. o-series reasoning
        models take `max_completion_tokens` (not `max_tokens`) and reject a non-default
        `temperature`, so both legacy fields are omitted for them."""
        if _is_reasoning_model(self._model):
            if req.max_tokens is not None:
                body["max_completion_tokens"] = req.max_tokens
            # temperature intentionally omitted: reasoning models allow only the default (1)
        else:
            body["temperature"] = req.temperature
            if req.max_tokens is not None:
                body["max_tokens"] = req.max_tokens

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body: dict[str, object] = {
            "model": self._model,
            "messages": [m.model_dump() for m in req.messages],
            "stream": True,
        }
        self._apply_sampling(body, req)

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
            "stream": False,
        }
        if req.json_mode:
            body["response_format"] = {"type": "json_object"}
        self._apply_sampling(body, req)

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
        except (ValueError, TypeError) as exc:  # a 200 with a non-JSON body
            raise ProviderError(f"provider returned an unparseable body: {exc}") from exc
        try:
            choices = data.get("choices") or []
            if not choices:
                raise ProviderError("provider returned no choices")
            return choices[0].get("message", {}).get("content") or ""
        except (AttributeError, TypeError, KeyError) as exc:
            raise ProviderError(f"provider returned an unexpected body shape: {exc}") from exc

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
