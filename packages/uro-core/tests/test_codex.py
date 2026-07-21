"""Codex (ChatGPT-subscription) provider — OAuth device flow + Responses-API adapter (D-47).

All offline via httpx.MockTransport; no live OpenAI calls (CI never does).
"""

import base64
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest
from uro_core.errors import ProviderError
from uro_core.ports.model_registry import ModelConnection
from uro_core.providers import codex_auth
from uro_core.providers.adapters.codex import CodexResponsesProvider
from uro_core.providers.base import CompletionRequest, Message
from uro_core.providers.registry import CodexTokenSource, classify_modality, discover_models


def _req(messages: list[Message] | None = None, **kw: object) -> CompletionRequest:
    msgs = messages or [Message(role="user", content="hi")]
    return CompletionRequest(messages=msgs, stage_tag="narrator", **kw)  # type: ignore[arg-type]


def _jwt(exp: int | None) -> str:
    """A minimal unsigned JWT carrying just an `exp` claim (what token_is_expiring reads)."""
    claims = json.dumps({"exp": exp} if exp is not None else {}).encode()
    body = base64.urlsafe_b64encode(claims).rstrip(b"=").decode()
    return f"h.{body}.sig"


# --- adapter: message translation ---------------------------------------------------------------


def test_body_translates_messages_to_responses_shape() -> None:
    prov = CodexResponsesProvider(model="gpt-5-codex", token_provider=_static("t"))
    body = prov._body(
        _req(
            messages=[
                Message(role="system", content="be terse"),
                Message(role="user", content="hello"),
                Message(role="assistant", content="hi"),
            ],
            max_tokens=64,
            json_mode=True,
        )
    )
    assert body["model"] == "gpt-5-codex"
    assert body["instructions"] == "be terse"  # system → instructions, not a message
    assert body["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "hi"}]},
    ]
    assert body["stream"] is True and body["store"] is False
    assert body["max_output_tokens"] == 64  # NOT max_tokens (Responses dialect)
    assert body["text"] == {"format": {"type": "json_object"}}  # json_mode
    assert "temperature" not in body  # gpt-5/codex reject a non-default temperature


def _static(token: str):  # type: ignore[no-untyped-def]
    async def _provider(force_refresh: bool = False) -> str:
        return token

    return _provider


# --- adapter: SSE parsing + streaming -----------------------------------------------------------


async def test_stream_parses_output_text_deltas() -> None:
    sse = (
        'data: {"type":"response.output_text.delta","delta":"Hel"}\n\n'
        'data: {"type":"response.created"}\n\n'
        'data: {"type":"response.output_text.delta","delta":"lo"}\n\n'
        "data: [DONE]\n\n"
    )
    prov = CodexResponsesProvider(
        model="gpt-5",
        token_provider=_static("t"),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=sse)),
    )
    assert "".join([c async for c in prov.stream(_req())]) == "Hello"


async def test_stream_raises_on_response_failed_event() -> None:
    sse = 'data: {"type":"response.failed","error":{"message":"boom"}}\n\n'
    prov = CodexResponsesProvider(
        model="gpt-5",
        token_provider=_static("t"),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=sse)),
    )
    with pytest.raises(ProviderError, match="boom"):
        async for _ in prov.stream(_req()):
            pass


async def test_stream_retries_once_on_401_with_a_forced_refresh() -> None:
    # A 401 on the first attempt must trigger token_provider(force_refresh=True) and a single retry.
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        n = len(calls)
        calls.append(n)
        if n == 0:
            return httpx.Response(401, text="unauthorized")
        return httpx.Response(
            200, text='data: {"type":"response.output_text.delta","delta":"ok"}\n\ndata: [DONE]\n\n'
        )

    forced: list[bool] = []

    async def token_provider(force_refresh: bool = False) -> str:
        forced.append(force_refresh)
        return "tok"

    prov = CodexResponsesProvider(
        model="gpt-5", token_provider=token_provider, transport=httpx.MockTransport(handler)
    )
    assert "".join([c async for c in prov.stream(_req())]) == "ok"
    assert forced == [False, True]  # normal attempt, then forced-refresh retry
    assert len(calls) == 2


async def test_stream_gives_up_after_a_second_401() -> None:
    prov = CodexResponsesProvider(
        model="gpt-5",
        token_provider=_static("t"),
        transport=httpx.MockTransport(lambda r: httpx.Response(401, text="no")),
    )
    with pytest.raises(ProviderError, match="reconnect"):
        async for _ in prov.stream(_req()):
            pass


async def test_complete_joins_the_stream() -> None:
    sse = (
        'data: {"type":"response.output_text.delta","delta":"a"}\n\n'
        'data: {"type":"response.output_text.delta","delta":"b"}\n\n'
        "data: [DONE]\n\n"
    )
    prov = CodexResponsesProvider(
        model="gpt-5",
        token_provider=_static("t"),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=sse)),
    )
    assert await prov.complete(_req()) == "ab"


async def test_embed_is_unsupported() -> None:
    prov = CodexResponsesProvider(model="gpt-5", token_provider=_static("t"))
    with pytest.raises(ProviderError, match="no embedding endpoint"):
        await prov.embed(["x"])


# --- OAuth device flow --------------------------------------------------------------------------


async def test_request_device_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/deviceauth/usercode")
        assert json.loads(request.content)["client_id"] == codex_auth.CLIENT_ID
        return httpx.Response(
            200, json={"device_auth_id": "dev1", "user_code": "J7DE-8NXJS", "interval": 5}
        )

    out = await codex_auth.request_device_code(transport=httpx.MockTransport(handler))
    assert out["user_code"] == "J7DE-8NXJS"
    assert out["device_auth_id"] == "dev1"
    assert out["verification_uri"].endswith("/codex/device")  # filled default


async def test_poll_pending_then_done() -> None:
    pending = httpx.MockTransport(lambda r: httpx.Response(403, text="not yet"))
    assert await codex_auth.poll_device_auth("dev1", "code", transport=pending) is None

    done = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"authorization_code": "AC", "code_verifier": "V"})
    )
    got = await codex_auth.poll_device_auth("dev1", "code", transport=done)
    assert got is not None and got["authorization_code"] == "AC" and got["code_verifier"] == "V"


async def test_poll_detects_cloudflare_block() -> None:
    blocked = httpx.MockTransport(lambda r: httpx.Response(403, text="Cloudflare error 10"))
    with pytest.raises(codex_auth.CodexAuthError, match="Cloudflare"):
        await codex_auth.poll_device_auth("dev1", "code", transport=blocked)


async def test_exchange_code_posts_the_grant() -> None:
    seen: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/oauth/token")
        seen.update(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT"})

    out = await codex_auth.exchange_code("AC", "V", transport=httpx.MockTransport(handler))
    assert out["access_token"] == "AT" and out["refresh_token"] == "RT"
    assert seen["grant_type"] == ["authorization_code"]
    assert seen["code"] == ["AC"] and seen["code_verifier"] == ["V"]
    assert seen["redirect_uri"] == [codex_auth.REDIRECT_URI]


async def test_refresh_rotates_the_token() -> None:
    seen: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": "AT2"})

    out = await codex_auth.refresh_access_token("RT", transport=httpx.MockTransport(handler))
    assert out["access_token"] == "AT2"
    assert seen["grant_type"] == ["refresh_token"] and seen["refresh_token"] == ["RT"]


async def test_token_endpoint_429_is_rate_limited() -> None:
    tx = httpx.MockTransport(lambda r: httpx.Response(429, text="slow down"))
    with pytest.raises(codex_auth.CodexRateLimited):
        await codex_auth.refresh_access_token("RT", transport=tx)


def test_token_is_expiring() -> None:
    assert codex_auth.token_is_expiring(_jwt(int(time.time()) - 10)) is True  # already expired
    assert codex_auth.token_is_expiring(_jwt(int(time.time()) + 3600)) is False  # good for an hour
    assert codex_auth.token_is_expiring("not-a-jwt") is True  # unreadable → refresh defensively


async def test_discover_codex_models_filters_and_sorts() -> None:
    payload = {
        "models": [
            {"slug": "gpt-5", "visibility": "public", "priority": 2},
            {"slug": "secret", "visibility": "hidden", "priority": 1},
            {"slug": "gpt-5-codex", "visibility": "public", "priority": 1},
        ]
    }
    tx = httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    out = await codex_auth.discover_codex_models("AT", transport=tx)
    assert [m["id"] for m in out] == ["gpt-5-codex", "gpt-5"]  # priority-sorted, hidden dropped
    assert all(m["modality"] == "chat" for m in out)


async def test_discover_codex_models_falls_back_on_error() -> None:
    tx = httpx.MockTransport(lambda r: httpx.Response(500, text="down"))
    out = await codex_auth.discover_codex_models("AT", transport=tx)
    assert [m["id"] for m in out] == codex_auth.DEFAULT_MODELS


# --- registry wiring ----------------------------------------------------------------------------


def test_classify_and_discover_registry_wiring() -> None:
    assert classify_modality("codex", "gpt-5-codex") == "chat"


async def test_discover_models_routes_codex() -> None:
    conn = ModelConnection(id="c1", name="cx", provider="codex", auth_id="a1")
    tx = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"models": [{"slug": "gpt-5", "visibility": "public"}]})
    )
    out = await discover_models(conn, "AT", transport=tx)
    assert out == [{"id": "gpt-5", "modality": "chat"}]


class _FakeStore:
    def __init__(self, access: str, refresh: str | None) -> None:
        self.access, self.refresh = access, refresh
        self.updated: tuple[str, str | None] | None = None

    async def get_secret(self, cid: str) -> tuple[str | None, str | None] | None:
        return (self.access, self.refresh)

    async def update_credential_tokens(
        self, cid: str, *, access_token: str, refresh_token: str | None = None
    ) -> None:
        self.updated = (access_token, refresh_token)
        self.access = access_token
        if refresh_token:
            self.refresh = refresh_token


async def test_token_source_refreshes_and_persists_when_expiring(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def fake_refresh(rt: str, **kw: object) -> dict[str, str]:
        return {"access_token": "NEW", "refresh_token": "NEWRT"}

    monkeypatch.setattr(codex_auth, "refresh_access_token", fake_refresh)
    store = _FakeStore(access=_jwt(int(time.time()) - 10), refresh="RT")  # expired
    src = CodexTokenSource(store, "a1")  # type: ignore[arg-type]
    assert await src() == "NEW"
    assert store.updated == ("NEW", "NEWRT")  # rotation persisted


async def test_token_source_returns_current_when_fresh(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def boom(rt: str, **kw: object) -> dict[str, str]:
        raise AssertionError("should not refresh a fresh token")

    monkeypatch.setattr(codex_auth, "refresh_access_token", boom)
    good = _jwt(int(time.time()) + 3600)
    src = CodexTokenSource(_FakeStore(access=good, refresh="RT"), "a1")  # type: ignore[arg-type]
    assert await src() == good


async def test_token_source_forces_refresh_on_401_retry(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def fake_refresh(rt: str, **kw: object) -> dict[str, str]:
        return {"access_token": "FORCED"}

    monkeypatch.setattr(codex_auth, "refresh_access_token", fake_refresh)
    good = _jwt(int(time.time()) + 3600)  # NOT expiring, but force_refresh should still refresh
    src = CodexTokenSource(_FakeStore(access=good, refresh="RT"), "a1")  # type: ignore[arg-type]
    assert await src(force_refresh=True) == "FORCED"
