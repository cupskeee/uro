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


async def test_complete_json_mode_prefills_open_brace_on_an_older_model() -> None:
    # json_mode prefills an assistant '{' so Claude returns a JSON object — on a model that
    # still takes a prefill (D-110).
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": '"actors":[],"claims":[]}'}]}
        )

    provider = AnthropicProvider(
        model="claude-haiku-4-5", api_key="k" * 12, transport=httpx.MockTransport(handler)
    )
    result = await provider.complete(_req(json_mode=True))
    assert result == '{"actors":[],"claims":[]}'
    assert seen["messages"][-1] == {"role": "assistant", "content": "{"}


def _body_for(model: str, *, json_mode: bool = False, effort: str | None = None) -> dict:
    return AnthropicProvider(model=model, api_key="k", reasoning_effort=effort)._body(
        _req(json_mode=json_mode), stream=False
    )


@pytest.mark.parametrize(
    ("model", "sampling", "prefill"),
    [
        ("claude-3-5-sonnet-20241022", True, True),
        ("claude-sonnet-4-20250514", True, True),
        ("claude-opus-4-1", True, True),
        ("claude-haiku-4-5-20251001", True, True),
        ("claude-sonnet-4-6", True, False),
        ("claude-opus-4-6", True, False),
        ("claude-opus-4-7", False, False),
        ("claude-opus-4-8", False, False),
        ("claude-sonnet-5", False, False),
        ("claude-sonnet-5-5", False, False),
        ("claude-haiku-5-5", False, False),
        ("claude-opus-5-5", False, False),
        ("claude-fable-5-1", False, False),
        ("some-proxy-alias", False, False),  # unknown → neither, which every model accepts
    ],
)
def test_sampling_and_prefill_only_where_the_model_takes_them(
    model: str, sampling: bool, prefill: bool
) -> None:
    # D-110 (Uro Loom request 7): current models 400 on `temperature` and on a prefill.
    from uro_core.providers.adapters.anthropic import _takes_prefill

    assert ("temperature" in _body_for(model)) is sampling
    assert _takes_prefill(model) is prefill


async def test_json_mode_on_a_current_model_sends_no_prefill() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": '{"a": 1}'}]})

    provider = AnthropicProvider(
        model="claude-sonnet-5-5", api_key="k", transport=httpx.MockTransport(handler)
    )
    assert await provider.complete(_req(json_mode=True)) == '{"a": 1}'
    assert [m["role"] for m in seen["messages"]] == ["user"]
    assert "temperature" not in seen


def test_reasoning_effort_goes_out_as_output_config() -> None:
    assert _body_for("claude-opus-5-5", effort="low")["output_config"] == {"effort": "low"}
    assert "output_config" not in _body_for("claude-opus-5-5")


async def test_a_refused_call_says_why_without_the_key() -> None:
    # D-110 (request 9): the status and Anthropic's own error, never the exception text.
    key = "sk-ant-secret-0123456789"
    body = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": f"temperature is not supported on this model (key {key})",
        },
    }
    provider = AnthropicProvider(
        model="claude-opus-5-5",
        api_key=key,
        transport=httpx.MockTransport(lambda request: httpx.Response(400, json=body)),
    )
    for call in (provider.complete(_req()), _drain(provider.stream(_req()))):
        with pytest.raises(ProviderError) as info:
            await call
        detail = info.value.detail
        assert detail is not None and detail.startswith("HTTP 400 invalid_request_error:")
        assert "temperature is not supported" in detail
        assert key not in detail and key not in str(info.value)


async def _drain(it) -> None:
    async for _ in it:
        pass


async def test_non_200_raises_provider_error() -> None:
    provider = _provider(lambda request: httpx.Response(400, json={"error": {"message": "bad"}}))
    with pytest.raises(ProviderError):
        await provider.complete(_req())


async def test_embed_raises() -> None:
    provider = _provider(lambda request: httpx.Response(200, json={}))
    with pytest.raises(ProviderError):
        await provider.embed(["anything"])


async def test_complete_raises_on_max_tokens_truncation() -> None:
    # A truncated (stop_reason=max_tokens) response returns HTTP 200 with partial JSON;
    # complete() must fail loudly so the extractor degrades instead of parsing garbage.
    data = {"stop_reason": "max_tokens", "content": [{"type": "text", "text": '"actors":['}]}
    provider = _provider(lambda request: httpx.Response(200, json=data))
    with pytest.raises(ProviderError, match="max_tokens"):
        await provider.complete(_req(json_mode=True))


async def test_complete_raises_on_non_json_body() -> None:
    # A gateway/proxy 200 with an HTML body must degrade to ProviderError, not crash.
    provider = _provider(lambda request: httpx.Response(200, text="<html>gateway</html>"))
    with pytest.raises(ProviderError):
        await provider.complete(_req())
