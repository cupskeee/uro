"""Anthropic Messages API provider (docs/04).

Different shape from OpenAI: the system prompt is a top-level field (not a message),
`max_tokens` is required, and streaming uses Anthropic's own SSE event types. No
embeddings endpoint — embed() raises, and the router binds an embedding-capable
provider to the `embedder` role instead.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx

from uro_core.errors import ProviderError
from uro_core.providers.base import CompletionRequest, Message

_VERSION = "2023-06-01"

logger = logging.getLogger(__name__)


def _split(messages: list[Message]) -> tuple[str | None, list[dict[str, str]]]:
    """Anthropic wants the system prompt separate from the user/assistant turns."""
    system = "\n\n".join(m.content for m in messages if m.role == "system") or None
    convo = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
    return system, convo


class AnthropicProvider:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        max_tokens: int = 2048,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": _VERSION,
            "content-type": "application/json",
        }

    def _body(
        self, req: CompletionRequest, *, stream: bool, convo: list[dict[str, str]] | None = None
    ) -> dict[str, object]:
        system, split_convo = _split(req.messages)
        body: dict[str, object] = {
            "model": self._model,
            "max_tokens": req.max_tokens or self._max_tokens,
            "messages": convo if convo is not None else split_convo,
            "temperature": req.temperature,
            "stream": stream,
        }
        if system is not None:
            body["system"] = system
        return body

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        try:
            async with (
                httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client,
                client.stream(
                    "POST",
                    f"{self._base_url}/v1/messages",
                    headers=self._headers(),
                    json=self._body(req, stream=True),
                ) as resp,
            ):
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if not data:
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    kind = event.get("type")
                    if kind == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield delta["text"]
                    elif kind == "message_delta":
                        # carries stop_reason; a max_tokens cutoff is a normal 200 stream.
                        if (event.get("delta") or {}).get("stop_reason") == "max_tokens":
                            logger.warning("anthropic narration truncated at max_tokens")
                    elif kind == "message_stop":
                        break
                    elif kind == "error":
                        err = event.get("error") or {}
                        raise ProviderError(f"anthropic stream error: {err.get('message', err)}")
        except httpx.HTTPError as exc:
            raise ProviderError(f"anthropic request failed: {exc}") from exc

    async def complete(self, req: CompletionRequest) -> str:
        prefill = ""
        convo: list[dict[str, str]] | None = None
        if req.json_mode:
            # Anthropic has no response_format; prefill an assistant "{" to force JSON.
            _, base = _split(req.messages)
            convo = [*base, {"role": "assistant", "content": "{"}]
            prefill = "{"
        body = self._body(req, stream=False, convo=convo)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(
                    f"{self._base_url}/v1/messages", headers=self._headers(), json=body
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            raise ProviderError(f"anthropic request failed: {exc}") from exc
        except (ValueError, TypeError) as exc:  # a 200 with a non-JSON body
            raise ProviderError(f"anthropic returned an unparseable body: {exc}") from exc
        if data.get("stop_reason") == "max_tokens":
            # truncated output → likely invalid JSON; fail loudly so the caller degrades.
            raise ProviderError("anthropic response hit max_tokens (output truncated)")
        try:
            blocks = data.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        except (AttributeError, TypeError, KeyError) as exc:
            raise ProviderError(f"anthropic returned an unexpected body shape: {exc}") from exc
        return prefill + text

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise ProviderError(
            "anthropic has no embeddings endpoint; bind the 'embedder' role to another provider"
        )
