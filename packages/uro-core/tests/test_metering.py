"""Metering (D-66): every call through the router is recorded with the tokens its adapter reported,
the world and branch it served, and — for a structured call — its full response.

Adapters are exercised against mock transports (CI never makes a live LLM call); the end-to-end
legs run a real beat into the test database.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.ids import new_id
from uro_core.errors import ProviderError
from uro_core.metering import RESPONSE_CAP, LLMCall, describe_failure
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.anthropic import AnthropicProvider
from uro_core.providers.adapters.codex import CodexResponsesProvider
from uro_core.providers.adapters.openai_compat import OpenAICompatProvider
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest, Message, report_usage
from uro_core.providers.router import ProviderRouter

MSG = [Message(role="user", content="hi")]


def _recording(provider: Any) -> tuple[ProviderRouter, list[LLMCall]]:
    calls: list[LLMCall] = []

    async def record(call: LLMCall) -> None:
        calls.append(call)

    return ProviderRouter(bindings={}, default=provider, recorder=record), calls


def _json(body: dict[str, Any], seen: list[dict[str, Any]] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def _sse(lines: list[dict[str, Any] | str], seen: list[dict[str, Any]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        text = "".join(
            f"data: {line if isinstance(line, str) else json.dumps(line)}\n\n" for line in lines
        )
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

    return httpx.MockTransport(handler)


async def test_openai_reports_usage_for_complete_stream_and_embed() -> None:
    complete = OpenAICompatProvider(
        model="gpt-x",
        transport=_json(
            {
                "choices": [{"message": {"content": '{"actors": []}'}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5},
            }
        ),
    )
    router, calls = _recording(complete)
    assert await router.complete("extractor", MSG, branch_id="b1") == '{"actors": []}'
    (call,) = calls
    assert (call.tokens_in, call.tokens_out, call.model) == (12, 5, "gpt-x")
    assert call.response == '{"actors": []}' and call.branch_id == "b1"

    seen: list[dict[str, Any]] = []
    frames: list[dict[str, Any] | str] = [
        {"choices": [{"delta": {"content": "The "}}]},
        {"choices": [{"delta": {"content": "tide."}}]},
        {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 2}},
        "[DONE]",
    ]
    official = OpenAICompatProvider(model="gpt-x", transport=_sse(frames, seen))
    router, calls = _recording(official)
    assert "".join([c async for c in router.stream("narrator", MSG)]) == "The tide."
    assert (calls[0].tokens_in, calls[0].tokens_out) == (30, 2)
    assert calls[0].response is None  # narration is the beat's own text, not stored again
    assert seen[0]["stream_options"] == {"include_usage": True}
    compat = OpenAICompatProvider(
        model="llama", base_url="http://local/v1", transport=_sse(frames, seen)
    )
    _ = [c async for c in _recording(compat)[0].stream("narrator", MSG)]
    assert "stream_options" not in seen[1]  # not every compatible server accepts it
    lookalike = OpenAICompatProvider(
        model="x", base_url="https://api.openai.com.example/v1", transport=_sse(frames, seen)
    )
    _ = [c async for c in _recording(lookalike)[0].stream("narrator", MSG)]
    assert "stream_options" not in seen[2]  # the HOST must be api.openai.com, not a prefix

    embed = OpenAICompatProvider(
        model="emb",
        transport=_json({"data": [{"embedding": [0.1]}], "usage": {"prompt_tokens": 4}}),
    )
    router, calls = _recording(embed)
    await router.embed("embedder", ["x"])
    assert (calls[0].tokens_in, calls[0].tokens_out) == (4, None)


async def test_anthropic_reports_usage_for_complete_and_stream() -> None:
    complete = AnthropicProvider(
        model="claude-x",
        api_key="k",
        transport=_json(
            {
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 9, "output_tokens": 3},
                "stop_reason": "end_turn",
            }
        ),
    )
    router, calls = _recording(complete)
    await router.complete("planner", MSG)
    assert (calls[0].tokens_in, calls[0].tokens_out) == (9, 3)

    seen: list[dict[str, Any]] = []
    frames: list[dict[str, Any] | str] = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 40, "output_tokens": 1}}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Rain."}},
        {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 6}},
        {"type": "message_stop"},
    ]
    stream = AnthropicProvider(model="claude-x", api_key="k", transport=_sse(frames, seen))
    router, calls = _recording(stream)
    _ = [c async for c in router.stream("narrator", MSG)]
    assert (calls[0].tokens_in, calls[0].tokens_out) == (40, 6)  # the cumulative output, once

    truncated = AnthropicProvider(
        model="claude-x",
        api_key="k",
        transport=_json(
            {
                "content": [{"type": "text", "text": '"actors": [{"na'}],
                "usage": {"input_tokens": 9, "output_tokens": 50},
                "stop_reason": "max_tokens",
            }
        ),
    )
    router, calls = _recording(truncated)
    with pytest.raises(ProviderError, match="max_tokens"):
        await router.complete("extractor", MSG, json_mode=True)
    # the cut-off output is exactly what an operator needs to see (review)
    assert calls[0].response == '{"actors": [{"na' and calls[0].tokens_out == 50


async def test_codex_reports_usage_from_response_completed() -> None:
    async def token(force: bool) -> str:
        return "t"

    seen: list[dict[str, Any]] = []
    frames: list[dict[str, Any] | str] = [
        {"type": "response.output_text.delta", "delta": "{}"},
        {
            "type": "response.completed",
            "response": {"usage": {"input_tokens": 7, "output_tokens": 2}},
        },
    ]
    codex = CodexResponsesProvider(
        model="gpt-5", token_provider=token, transport=_sse(frames, seen)
    )
    router, calls = _recording(codex)
    assert await router.complete("extractor", MSG) == "{}"
    assert (calls[0].tokens_in, calls[0].tokens_out) == (7, 2)


class _Fake:
    """A provider that reports what it's told — or fails."""

    def __init__(self, reply: str = "{}", tokens: int = 1, fail: bool = False) -> None:
        self.reply, self.tokens, self.fail = reply, tokens, fail

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield self.reply

    async def complete(self, req: CompletionRequest) -> str:
        await asyncio.sleep(0)  # let concurrent calls interleave
        report_usage(tokens_in=self.tokens, tokens_out=self.tokens)
        if self.fail:
            raise RuntimeError("provider down")
        return self.reply

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


async def test_the_router_meters_failures_caps_responses_and_keeps_calls_apart() -> None:
    router, calls = _recording(_Fake(fail=True))
    with pytest.raises(RuntimeError):
        await router.complete("extractor", MSG)
    assert calls[0].response is None and calls[0].tokens_in == 1  # metered, nothing to store

    router, calls = _recording(_Fake(reply="x" * (RESPONSE_CAP + 10)))
    await router.complete("extractor", MSG)
    assert len(calls[0].response or "") == RESPONSE_CAP

    bindings = {"a": _Fake(tokens=3), "b": _Fake(tokens=50)}
    recorded: list[LLMCall] = []

    async def record(call: LLMCall) -> None:
        recorded.append(call)

    router = ProviderRouter(bindings=bindings, recorder=record)
    await asyncio.gather(router.complete("a", MSG), router.complete("b", MSG))
    assert {c.stage_tag: c.tokens_in for c in recorded} == {"a": 3, "b": 50}  # never mixed

    async def broken(call: LLMCall) -> None:
        raise RuntimeError("db down")

    assert await ProviderRouter(bindings={}, default=_Fake(), recorder=broken).complete("x", MSG)


class _Story(_Fake):
    async def complete(self, req: CompletionRequest) -> str:
        report_usage(tokens_in=100, tokens_out=20)
        if req.stage_tag == "extractor":
            return '{"actors": [{"name": "Mara"}], "claims": []}'
        return "{}"


async def test_a_beat_records_its_calls_with_world_branch_and_response(
    store: PostgresEventStore,
) -> None:
    world = await store.create_world(f"Metered-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    engine = Engine(store, ProviderRouter(bindings={}, default=_Story(reply="Mara waves.")))
    await engine.run_beat(campaign, "p1", "I wave at Mara")

    calls = await store.recent_llm_calls(world_id=world.world_id, limit=50)
    stages = {c["stage_tag"] for c in calls}
    assert {"narrator", "extractor", "embedder"} <= stages
    assert all(c["branch_id"] == campaign.branch_id for c in calls)
    (extractor,) = [c for c in calls if c["stage_tag"] == "extractor"]
    assert "Mara" in extractor["response"] and extractor["tokens_in"] == 100
    assert all(c["response"] is None for c in calls if c["stage_tag"] != "extractor")

    by_world = await store.usage_by_stage(world_id=world.world_id)
    assert {r["stage_tag"] for r in by_world} == stages
    by_branch = await store.usage_by_stage(branch_ids=[campaign.branch_id], stage="extractor")
    assert by_branch[0]["tokens_in"] == 100 and by_branch[0]["calls"] == 1
    other = await store.create_world(f"Quiet-{new_id()}")
    assert await store.usage_by_stage(world_id=other.world_id) == []


async def test_a_call_meters_into_its_engine_s_store_not_the_router_s(
    store: PostgresEventStore,
) -> None:
    # One router can serve engines over different stores; each call is recorded by the store of
    # the engine that made it, never by whichever recorder the router was built with (review).
    elsewhere: list[LLMCall] = []

    async def record_elsewhere(call: LLMCall) -> None:
        elsewhere.append(call)

    router = ProviderRouter(
        bindings={}, default=_Story(reply="Mara waves."), recorder=record_elsewhere
    )
    world = await store.create_world(f"Own-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    await Engine(store, router).run_beat(campaign, "p1", "I wave at Mara")
    assert elsewhere == []
    assert await store.recent_llm_calls(world_id=world.world_id)


async def test_a_connection_probe_is_metered(store: PostgresEventStore) -> None:
    from uro_core.providers.registry import probe_connection

    conn = await store.add_connection(name=f"stub-{new_id()}", provider="stub")
    try:
        before = len(await store.recent_llm_calls(stage="test", limit=10_000))
        assert (await probe_connection(store, conn, "stub-chat"))["ok"]
        assert len(await store.recent_llm_calls(stage="test", limit=10_000)) == before + 1
    finally:
        await store.delete_connection(conn)


def test_a_failure_is_described_by_type_never_by_its_text() -> None:
    # Live run (2026-10-31): a planner call failed in 22 ms and left nothing to read. A failure now
    # records its exception types and any HTTP status, but never the exception's text, which can
    # carry a credential (an illegal-header error embeds the Authorization value).
    class _Response:
        status_code = 429

    class HTTPStatusError(Exception):
        response = _Response()

    try:
        try:
            raise HTTPStatusError("Bearer sk-secret rejected")
        except HTTPStatusError as cause:
            raise ProviderError("provider request failed: Bearer sk-secret") from cause
    except ProviderError as exc:
        described = describe_failure(exc)
    assert described == "ProviderError ← HTTPStatusError (HTTP 429)"
    assert "sk-secret" not in described


async def test_a_failed_call_records_why(store: PostgresEventStore) -> None:
    router, calls = _recording(_Fake(fail=True))
    with pytest.raises(RuntimeError):
        await router.complete("extractor", MSG)
    assert calls[0].error == "RuntimeError"
    router, calls = _recording(_Fake())
    await router.complete("extractor", MSG)
    assert calls[0].error is None  # a success records no error

    world = await store.create_world(f"Failing-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    router = ProviderRouter(bindings={}, default=_Fake(fail=True), recorder=store.record_llm_call)
    with pytest.raises(RuntimeError):
        await router.complete("planner", MSG, branch_id=campaign.branch_id)
    (row,) = await store.recent_llm_calls(world_id=world.world_id)
    assert row["error"] == "RuntimeError"
    (usage,) = await store.usage_by_stage(world_id=world.world_id)
    assert (usage["calls"], usage["failed"]) == (1, 1)


class _Breaks:
    """Fails mid-stream and on embed; `slow` hangs a complete so it can be cancelled."""

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "The "
        raise RuntimeError("stream dropped")

    async def complete(self, req: CompletionRequest) -> str:
        await asyncio.sleep(10)
        return "{}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedder down")


async def test_every_kind_of_call_records_its_failure() -> None:
    # Review: only `complete` was tested, and a cancelled call (a server stopping mid-beat) was
    # recorded with no error, i.e. as a success.
    router, calls = _recording(_Breaks())
    with pytest.raises(RuntimeError):
        _ = [c async for c in router.stream("narrator", MSG)]
    with pytest.raises(RuntimeError):
        await router.embed("embedder", ["x"])
    task = asyncio.create_task(router.complete("planner", MSG))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [(c.stage_tag, c.error) for c in calls] == [
        ("narrator", "RuntimeError"),
        ("embedder", "RuntimeError"),
        ("planner", "CancelledError"),
    ]

    router, calls = _recording(_Fake())
    _ = [c async for c in router.stream("narrator", MSG)]  # a consumer reading to the end
    stream = router.stream("narrator", MSG)
    await anext(stream)
    await stream.aclose()  # a consumer stopping early is not a failure
    assert [c.error for c in calls] == [None, None]


async def test_a_codex_http_error_records_its_status() -> None:
    # Review: the Codex adapter raised its own ProviderError for an HTTP error with nothing
    # chained, so the live run's chat provider recorded a bare "ProviderError" with no status.
    async def token(force: bool) -> str:
        return "t"

    codex = CodexResponsesProvider(
        model="gpt-5",
        token_provider=token,
        transport=httpx.MockTransport(lambda r: httpx.Response(429, json={"error": "slow down"})),
    )
    router, calls = _recording(codex)
    with pytest.raises(ProviderError):
        await router.complete("planner", MSG)
    assert calls[0].error == "ProviderError ← HTTPStatusError (HTTP 429)"
