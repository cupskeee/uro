"""openai_compat SSE parsing — the one real network component, tested with a mock transport."""

import httpx
import pytest
from uro_core.errors import ProviderError
from uro_core.providers.adapters.openai_compat import OpenAICompatProvider
from uro_core.providers.base import CompletionRequest, Message


def _transport(sse_body: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse_body)

    return httpx.MockTransport(handler)


def _req() -> CompletionRequest:
    return CompletionRequest(messages=[Message(role="user", content="hi")], stage_tag="narrator")


def _provider(sse_body: str) -> OpenAICompatProvider:
    return OpenAICompatProvider(model="m", base_url="http://x/v1", transport=_transport(sse_body))


async def test_streams_content_deltas_until_done() -> None:
    sse = (
        'data: {"choices":[{"delta":{"content":"Hello "}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"world"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    chunks = [c async for c in _provider(sse).stream(_req())]
    assert "".join(chunks) == "Hello world"


async def test_raises_on_in_band_error_frame() -> None:
    # Content streams, then the provider emits an error frame mid-stream (HTTP was 200).
    sse = (
        'data: {"choices":[{"delta":{"content":"Hello "}}]}\n\n'
        'data: {"error":{"message":"upstream exploded","type":"server_error"}}\n\n'
        "data: [DONE]\n\n"
    )
    got: list[str] = []
    with pytest.raises(ProviderError, match="upstream exploded"):
        async for chunk in _provider(sse).stream(_req()):
            got.append(chunk)
    assert got == ["Hello "]  # partial content seen, but the stream aborts loudly


async def test_raises_on_non_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    provider = OpenAICompatProvider(
        model="m", base_url="http://x/v1", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ProviderError):
        async for _ in provider.stream(_req()):
            pass
