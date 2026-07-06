"""Anthropic Messages API adapter — SSE parsing and request shaping, via mock transport."""

import httpx
import pytest
from uro_core.errors import ProviderError
from uro_core.providers.adapters.anthropic import AnthropicProvider
from uro_core.providers.base import CompletionRequest, Message


def _provider(handler) -> AnthropicProvider:
    return AnthropicProvider(
        model="claude-x", api_key="test-key", transport=httpx.MockTransport(handler)
    )


def _req(json_mode: bool = False) -> CompletionRequest:
    return CompletionRequest(
        messages=[
            Message(role="system", content="You are a narrator."),
            Message(role="user", content="Describe the tavern."),
        ],
        stage_tag="narrator",
        json_mode=json_mode,
    )


async def test_stream_yields_text_deltas() -> None:
    sse = (
        'event: content_block_delta\ndata: {"type":"content_block_delta",'
        '"delta":{"type":"text_delta","text":"A warm "}}\n\n'
        'event: content_block_delta\ndata: {"type":"content_block_delta",'
        '"delta":{"type":"text_delta","text":"tavern."}}\n\n'
        'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    provider = _provider(lambda request: httpx.Response(200, text=sse))
    chunks = [c async for c in provider.stream(_req())]
    assert "".join(chunks) == "A warm tavern."


async def test_stream_puts_system_top_level_and_drops_system_message() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, text='event: message_stop\ndata: {"type":"message_stop"}\n\n')

    _ = [c async for c in _provider(handler).stream(_req())]
    assert seen["system"] == "You are a narrator."  # system is top-level, not a message
    assert [m["role"] for m in seen["messages"]] == ["user"]
    assert seen["max_tokens"] > 0  # required by the API


async def test_complete_joins_text_blocks() -> None:
    data = {"content": [{"type": "text", "text": "It is quiet."}]}
    provider = _provider(lambda request: httpx.Response(200, json=data))
    assert await provider.complete(_req()) == "It is quiet."


async def test_complete_json_mode_prefills_open_brace() -> None:
    # json_mode prefills an assistant '{' so Claude returns a JSON object.
    data = {"content": [{"type": "text", "text": '"actors":[],"claims":[]}'}]}
    provider = _provider(lambda request: httpx.Response(200, json=data))
    result = await provider.complete(_req(json_mode=True))
    assert result == '{"actors":[],"claims":[]}'


async def test_non_200_raises_provider_error() -> None:
    provider = _provider(lambda request: httpx.Response(400, json={"error": {"message": "bad"}}))
    with pytest.raises(ProviderError):
        await provider.complete(_req())


async def test_embed_raises() -> None:
    provider = _provider(lambda request: httpx.Response(200, json={}))
    with pytest.raises(ProviderError):
        await provider.embed(["anything"])
