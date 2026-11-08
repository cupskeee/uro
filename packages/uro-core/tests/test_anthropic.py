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


# --- structured outputs and room for thinking ---------------------------------------------------

_SCHEMA = {
    "$defs": {
        "Item": {
            "properties": {
                "name": {"title": "Name", "type": "string", "minLength": 1},
                "note": {"default": "", "title": "Note", "type": "string"},
            },
            "required": ["name"],
            "title": "Item",
            "type": "object",
        }
    },
    "properties": {
        "items": {"items": {"$ref": "#/$defs/Item"}, "title": "Items", "type": "array"},
        "speaker": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
    },
    "title": "Out",
    "type": "object",
}


def _schema_req() -> CompletionRequest:
    return _req(json_mode=True).model_copy(update={"json_schema": _SCHEMA})


def _recording(model: str, replies: list[httpx.Response]) -> tuple[AnthropicProvider, list[dict]]:
    import json

    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return replies[min(len(bodies), len(replies)) - 1]

    return AnthropicProvider(
        model=model, api_key="k" * 12, transport=httpx.MockTransport(handler)
    ), bodies


def _ok(text: str = '{"items": []}') -> httpx.Response:
    return httpx.Response(200, json={"content": [{"type": "text", "text": text}]})


def test_strict_schema_closes_objects_and_keeps_optional_fields_optional() -> None:
    from uro_core.providers.adapters.anthropic import strict_schema

    out = strict_schema(_SCHEMA)
    item = out["$defs"]["Item"]
    assert out["additionalProperties"] is False and item["additionalProperties"] is False
    assert item["required"] == ["name"]  # `note` stays optional
    assert "minLength" not in item["properties"]["name"]  # moved where the model can read it
    assert "minLength: 1" in item["properties"]["name"]["description"]
    assert "default" not in item["properties"]["note"]
    assert out["properties"]["speaker"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert out["properties"]["items"]["items"] == {"$ref": "#/$defs/Item"}


def test_the_engine_stage_schemas_convert() -> None:
    from uro_core.pipeline.extraction import Extraction
    from uro_core.pipeline.plan import BeatPlan
    from uro_core.providers.adapters.anthropic import strict_schema

    for model in (BeatPlan, Extraction):
        out = strict_schema(model.model_json_schema())

        def closed(node: object) -> None:
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert node["additionalProperties"] is False
                assert "default" not in node
                for v in node.values():
                    closed(v)
            elif isinstance(node, list):
                for v in node:
                    closed(v)

        closed(out)


@pytest.mark.parametrize(
    ("model", "takes"),
    [
        ("claude-opus-5-5", True),
        ("claude-sonnet-5-5", True),
        ("claude-haiku-5-5", True),
        ("claude-fable-5-1", True),
        ("claude-opus-4-8", True),
        ("claude-haiku-4-5-20251001", True),
        ("claude-opus-4-1-20250805", True),
        ("claude-opus-4-7", False),
        ("claude-sonnet-4-6", False),
        ("claude-3-5-haiku-20241022", False),
        ("some-proxy-alias", False),  # unknown → no format: the safe direction for this field
    ],
)
async def test_a_schema_goes_out_only_to_the_models_that_take_one(model: str, takes: bool) -> None:
    provider, bodies = _recording(model, [_ok()])
    await provider.complete(_schema_req())
    fmt = (bodies[0].get("output_config") or {}).get("format")
    assert (fmt is not None) is takes
    if takes:
        assert fmt["type"] == "json_schema" and fmt["schema"]["additionalProperties"] is False
        assert bodies[0]["messages"][-1]["role"] == "user"  # never a prefill beside a format


async def test_a_schema_rides_with_the_effort() -> None:
    import json

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _ok()

    provider = AnthropicProvider(
        model="claude-opus-5-5",
        api_key="k",
        transport=httpx.MockTransport(handler),
        reasoning_effort="low",
    )
    await provider.complete(_schema_req())
    assert seen[0]["output_config"]["effort"] == "low" and "format" in seen[0]["output_config"]


async def test_no_schema_without_json_mode_or_without_one_given() -> None:
    provider, bodies = _recording("claude-opus-5-5", [_ok(), _ok()])
    await provider.complete(_req(json_mode=True))
    await provider.complete(_req().model_copy(update={"json_schema": _SCHEMA}))
    assert all("output_config" not in b for b in bodies)


async def test_a_refused_schema_falls_back_once_and_is_not_sent_again() -> None:
    refused = httpx.Response(
        400,
        json={"error": {"type": "invalid_request_error", "message": "schema too complex"}},
    )
    provider, bodies = _recording("claude-sonnet-5-5", [refused, _ok(), _ok()])
    assert await provider.complete(_schema_req()) == '{"items": []}'
    assert "output_config" in bodies[0] and "output_config" not in bodies[1]
    await provider.complete(_schema_req())  # the connection now sends plain JSON mode
    assert len(bodies) == 3 and "output_config" not in bodies[2]


async def test_a_400_that_isnt_the_schema_keeps_the_schema() -> None:
    bad = httpx.Response(
        400, json={"error": {"type": "invalid_request_error", "message": "bad effort"}}
    )
    provider, bodies = _recording("claude-sonnet-5-5", [bad, bad, _ok()])
    with pytest.raises(ProviderError):
        await provider.complete(_schema_req())
    await provider.complete(_schema_req())
    assert len(bodies) == 3 and "format" in bodies[2]["output_config"]


async def test_a_server_error_is_not_retried_as_a_schema_refusal() -> None:
    provider, bodies = _recording("claude-sonnet-5-5", [httpx.Response(529, json={})])
    with pytest.raises(ProviderError):
        await provider.complete(_schema_req())
    assert len(bodies) == 1


@pytest.mark.parametrize(
    ("model", "extra"),
    [
        ("claude-opus-5-5", 12_000),
        ("claude-sonnet-5-5", 12_000),
        ("claude-haiku-5-5", 12_000),
        ("claude-fable-5-1", 12_000),
        ("claude-opus-4-8", 0),  # thinks only when asked
        ("claude-haiku-4-5", 0),
        ("some-proxy-alias", 0),  # its output cap is unknown: never raise it
    ],
)
def test_a_thinking_by_default_model_gets_room_to_think(model: str, extra: int) -> None:
    req = _req().model_copy(update={"max_tokens": 1024})
    provider = AnthropicProvider(model=model, api_key="k")
    assert provider._body(req, stream=False)["max_tokens"] == 1024 + extra
    # narration (no stage cap) gets the same room on top of the adapter's default
    assert provider._body(_req(), stream=True)["max_tokens"] == 2048 + extra
