"""D-47 (docs/20): the instance-level, DB-backed model-connection registry — connections, encrypted
credentials, and role bindings, plus resolving them into a ProviderRouter. Deterministic (no live
model — the `stub` provider kind exercises the wiring). The credential/router tests need a KEK; the
CRUD/FK tests are DB-only. Registry rows are OFF THE BRANCH/EVENT AXIS (an operational table).
"""

import pytest
from cryptography.fernet import Fernet
from uro_core.adapters.crypto import SecretsUnavailable, decrypt_secret, encrypt_secret
from uro_core.adapters.postgres.store import PostgresEventStore

# --- crypto (unit, no DB) --------------------------------------------------------


def test_crypto_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("URO_SECRET_KEY", Fernet.generate_key().decode())
    ciphertext = encrypt_secret("sk-super-secret")
    assert ciphertext != "sk-super-secret"  # actually encrypted, not stored plaintext
    assert decrypt_secret(ciphertext) == "sk-super-secret"


def test_crypto_fails_closed_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("URO_SECRET_KEY", raising=False)
    with pytest.raises(SecretsUnavailable):
        encrypt_secret("sk-x")  # refuse to persist plaintext when no KEK is configured


def test_crypto_rejects_wrong_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("URO_SECRET_KEY", Fernet.generate_key().decode())
    ciphertext = encrypt_secret("sk-x")
    monkeypatch.setenv("URO_SECRET_KEY", Fernet.generate_key().decode())  # rotate → can't decrypt
    with pytest.raises(SecretsUnavailable):
        decrypt_secret(ciphertext)


# --- registry CRUD + FK behavior (DB) --------------------------------------------
#
# The `store` fixture does NOT roll back, so these tests clean up their own rows — leftover
# role_bindings would otherwise be picked up by a later `uro serve` and hijack its provider router.


async def test_connection_and_binding_crud(store: PostgresEventStore) -> None:
    cid = await store.add_connection(name="local ollama", provider="local")
    try:
        conns = await store.list_connections()
        assert any(c.id == cid and c.name == "local ollama" and c.is_enabled for c in conns)
        assert (await store.get_connection(cid)).provider == "local"  # type: ignore[union-attr]

        await store.set_role_binding("default", cid, "llama3.1")
        await store.set_role_binding("narrator", cid, "llama3.1")
        roles = {b.role: b for b in await store.list_role_bindings()}
        assert roles["default"].connection_id == cid and roles["narrator"].model == "llama3.1"

        # bind is an upsert (one row per role).
        await store.set_role_binding("narrator", cid, "qwen2.5")
        roles = {b.role: b for b in await store.list_role_bindings()}
        assert roles["narrator"].model == "qwen2.5"

        assert await store.delete_role_binding("narrator") is True
        assert "narrator" not in {b.role for b in await store.list_role_bindings()}
        assert await store.delete_role_binding("narrator") is False  # already gone → no-op
    finally:
        await store.delete_connection(cid)  # cascades the `default` binding


async def test_credentials_are_encrypted_at_rest(
    store: PostgresEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("URO_SECRET_KEY", Fernet.generate_key().decode())
    cred_id = await store.add_credential(provider="openai", access_token="sk-plaintext-123")
    try:
        # The raw column holds CIPHERTEXT, never the plaintext.
        async with store.pool.acquire() as conn:
            raw = await conn.fetchval(
                "SELECT access_token FROM provider_credentials WHERE id = $1", cred_id
            )
        assert raw != "sk-plaintext-123"
        assert decrypt_secret(raw) == "sk-plaintext-123"

        # list_credentials exposes METADATA only — never the secret.
        creds = await store.list_credentials()
        meta = next(c for c in creds if c.id == cred_id)
        assert meta.provider == "openai" and meta.has_access_token is True
        assert "sk-plaintext-123" not in meta.model_dump_json()

        # get_secret decrypts for the wiring layer.
        secret = await store.get_secret(cred_id)
        assert secret == ("sk-plaintext-123", None)
        assert await store.get_secret("nope") is None
    finally:
        await store.delete_credential(cred_id)


async def test_delete_credential_sets_connection_auth_null(
    store: PostgresEventStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("URO_SECRET_KEY", Fernet.generate_key().decode())
    cred_id = await store.add_credential(provider="openai", access_token="sk-x")
    conn_id = await store.add_connection(name="oai", provider="openai", auth_id=cred_id)
    try:
        assert await store.delete_credential(cred_id) is True
        survivor = await store.get_connection(conn_id)
        assert survivor is not None and survivor.auth_id is None  # UNLINKED, not cascade-deleted
    finally:
        await store.delete_connection(conn_id)


async def test_delete_connection_cascades_its_bindings(store: PostgresEventStore) -> None:
    conn_id = await store.add_connection(name="stub", provider="stub")
    await store.set_role_binding("narrator", conn_id, "n/a")
    assert await store.delete_connection(conn_id) is True  # cascade removes the binding + cleans up
    assert "narrator" not in {b.role for b in await store.list_role_bindings()}


# --- slice 3: model discovery + modality + provider construction ------------------

from uro_core.ports.model_registry import ModelConnection  # noqa: E402
from uro_core.providers.adapters.anthropic import AnthropicProvider  # noqa: E402
from uro_core.providers.adapters.openai_compat import OpenAICompatProvider  # noqa: E402
from uro_core.providers.adapters.stub import StubProvider  # noqa: E402
from uro_core.providers.registry import (  # noqa: E402
    classify_modality,
    discover_models,
    provider_from_connection,
)


def test_classify_modality() -> None:
    assert classify_modality("openai", "text-embedding-3-small") == "embedding"
    assert classify_modality("openai", "gpt-4o") == "chat"
    assert classify_modality("local", "nomic-embed-text") == "embedding"
    assert classify_modality("anthropic", "claude-sonnet-5") == "chat"  # no embedding models
    assert classify_modality("mystery", "whatever") == "unknown"  # unclassifiable provider


async def test_discover_models_stub_is_canned() -> None:
    conn = ModelConnection(id="c", name="stub", provider="stub")
    models = await discover_models(conn, None)
    assert {m["id"]: m["modality"] for m in models} == {
        "stub-chat": "chat",
        "stub-embed": "embedding",
    }


def test_provider_from_connection_injects_creds_and_base_url() -> None:
    oai = provider_from_connection(
        ModelConnection(id="c1", name="o", provider="openai"), "gpt-4o", "sk-x"
    )
    assert isinstance(oai, OpenAICompatProvider) and oai._api_key == "sk-x"
    ant = provider_from_connection(
        ModelConnection(id="c2", name="a", provider="anthropic", base_url="https://p"),
        "claude-sonnet-5",
        "sk-y",
    )
    assert isinstance(ant, AnthropicProvider) and ant._base_url == "https://p"
    assert isinstance(
        provider_from_connection(ModelConnection(id="c3", name="s", provider="stub"), "m", None),
        StubProvider,
    )
    with pytest.raises(ValueError):  # openai_compat needs a base_url
        provider_from_connection(
            ModelConnection(id="c4", name="x", provider="openai_compat"), "m", None
        )


async def test_set_connection_models_persists_cached_models(store: PostgresEventStore) -> None:
    cid = await store.add_connection(name="oai", provider="openai")
    try:
        models = [
            {"id": "gpt-4o", "modality": "chat"},
            {"id": "text-embedding-3", "modality": "embedding"},
        ]
        assert await store.set_connection_models(cid, models) is True
        got = await store.get_connection(cid)
        assert got is not None and got.cached_models == models  # jsonb round-trips
        assert await store.set_connection_models("nope", models) is False
    finally:
        await store.delete_connection(cid)
