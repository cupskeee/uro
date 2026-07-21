"""openai_compat SSE parsing — the one real network component, tested with a mock transport."""

import json

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


def _capturing(seen: dict[str, object]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    return httpx.MockTransport(handler)


async def test_reasoning_model_uses_max_completion_tokens_and_omits_temperature() -> None:
    # o-series reasoning models 400 on `max_tokens` (need `max_completion_tokens`) and on any
    # non-default `temperature` — the o4-mini `test`-probe failure. Assert the wire body switches.
    seen: dict[str, object] = {}
    provider = OpenAICompatProvider(
        model="o4-mini", base_url="http://x/v1", transport=_capturing(seen)
    )
    out = await provider.complete(
        CompletionRequest(
            messages=[Message(role="user", content="hi")], stage_tag="t", max_tokens=1
        )
    )
    assert out == "ok"
    assert seen["max_completion_tokens"] == 1
    assert "max_tokens" not in seen
    assert "temperature" not in seen


async def test_reasoning_model_stream_body_omits_all_sampling_when_uncapped() -> None:
    # The narrator uses stream() with max_tokens=None (router default). For a reasoning model that
    # combination sends NEITHER a temperature NOR a token cap — assert the streamed body too, since
    # complete() and stream() are separate wire-assembly paths.
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text="data: [DONE]\n\n")

    provider = OpenAICompatProvider(
        model="o3-mini", base_url="http://x/v1", transport=httpx.MockTransport(handler)
    )
    async for _ in provider.stream(
        CompletionRequest(messages=[Message(role="user", content="hi")], stage_tag="narrator")
    ):
        pass
    assert "temperature" not in seen
    assert "max_tokens" not in seen
    assert "max_completion_tokens" not in seen


async def test_reasoning_model_detected_through_a_gateway_namespace() -> None:
    # A gateway-namespaced id (OpenRouter `openai/o4-mini`) must still be recognised as reasoning.
    seen: dict[str, object] = {}
    provider = OpenAICompatProvider(
        model="openai/o4-mini", base_url="http://x/v1", transport=_capturing(seen)
    )
    await provider.complete(
        CompletionRequest(
            messages=[Message(role="user", content="hi")], stage_tag="t", max_tokens=5
        )
    )
    assert seen["max_completion_tokens"] == 5
    assert "max_tokens" not in seen
    assert "temperature" not in seen


async def test_chat_model_keeps_max_tokens_and_temperature() -> None:
    # A normal chat model (gpt-4o) is unchanged — legacy `max_tokens` + `temperature`.
    seen: dict[str, object] = {}
    provider = OpenAICompatProvider(
        model="gpt-4o", base_url="http://x/v1", transport=_capturing(seen)
    )
    await provider.complete(
        CompletionRequest(
            messages=[Message(role="user", content="hi")], stage_tag="t", max_tokens=7
        )
    )
    assert seen["max_tokens"] == 7
    assert "max_completion_tokens" not in seen
    assert "temperature" in seen
