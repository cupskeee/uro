"""D-47 (docs/20): `build_router_from_registry` resolves DB model-connection registry rows into a
ProviderRouter. The resolution logic is pure, so it's tested against an in-memory fake registry (no
DB) — the DB CRUD/crypto themselves live in uro-core's test_model_registry.py.
"""

import pytest
from uro_cli.wiring import build_router_from_registry
from uro_core.ports.model_registry import ModelConnection, RoleBinding
from uro_core.providers.adapters.anthropic import AnthropicProvider
from uro_core.providers.adapters.openai_compat import OpenAICompatProvider
from uro_core.providers.adapters.stub import StubProvider


class FakeRegistry:
    """The subset of the ModelRegistry port that build_router_from_registry actually calls."""

    def __init__(self, connections=None, bindings=None, secrets=None):  # type: ignore[no-untyped-def]
        self._connections = connections or []
        self._bindings = bindings or []
        self._secrets = secrets or {}

    async def list_role_bindings(self):  # type: ignore[no-untyped-def]
        return self._bindings

    async def list_connections(self):  # type: ignore[no-untyped-def]
        return self._connections

    async def get_secret(self, credential_id):  # type: ignore[no-untyped-def]
        return self._secrets.get(credential_id)


async def test_empty_registry_returns_none() -> None:
    # No bindings → None, so `serve` falls back to the --provider/uro.toml seed.
    assert await build_router_from_registry(FakeRegistry()) is None


async def test_resolves_roles_with_default_fallback() -> None:
    conn = ModelConnection(id="c1", name="stub", provider="stub")
    reg = FakeRegistry(
        connections=[conn],
        bindings=[
            RoleBinding(role="default", connection_id="c1", model="stub"),
            RoleBinding(role="narrator", connection_id="c1", model="stub"),
        ],
    )
    router = await build_router_from_registry(reg)
    assert router is not None
    assert isinstance(router._provider_for("narrator"), StubProvider)  # explicitly bound
    assert isinstance(router._provider_for("judge"), StubProvider)  # unbound → the default binding


async def test_disabled_or_missing_connection_is_skipped() -> None:
    reg = FakeRegistry(
        connections=[
            ModelConnection(id="c1", name="def", provider="stub"),
            ModelConnection(id="c2", name="off", provider="stub", is_enabled=False),
        ],
        bindings=[
            RoleBinding(role="default", connection_id="c1", model="stub"),
            RoleBinding(role="narrator", connection_id="c2", model="stub"),  # disabled → skip
            RoleBinding(role="planner", connection_id="ghost", model="stub"),  # missing → skip
        ],
    )
    router = await build_router_from_registry(reg)
    assert router is not None
    # Both skipped roles fall through to the enabled default.
    assert router._provider_for("narrator") is router._provider_for("judge")
    assert router._provider_for("planner") is router._provider_for("judge")


async def test_builds_providers_with_decrypted_creds_and_base_url() -> None:
    reg = FakeRegistry(
        connections=[
            ModelConnection(id="c1", name="oai", provider="openai", auth_id="k1"),
            ModelConnection(
                id="c2", name="ant", provider="anthropic", auth_id="k2", base_url="https://proxy"
            ),
        ],
        bindings=[
            RoleBinding(role="default", connection_id="c1", model="gpt-4o"),
            RoleBinding(role="dialogue", connection_id="c2", model="claude-sonnet-5"),
        ],
        secrets={"k1": ("sk-oai", None), "k2": ("sk-ant", None)},
    )
    router = await build_router_from_registry(reg)
    assert router is not None
    oai = router._provider_for("default")
    ant = router._provider_for("dialogue")
    assert isinstance(oai, OpenAICompatProvider)
    assert oai._api_key == "sk-oai" and oai._model == "gpt-4o"  # decrypted key injected
    assert isinstance(ant, AnthropicProvider)
    assert ant._api_key == "sk-ant" and ant._base_url == "https://proxy"  # per-connection override


async def test_openai_compat_without_base_url_is_an_error() -> None:
    reg = FakeRegistry(
        connections=[ModelConnection(id="c1", name="compat", provider="openai_compat")],
        bindings=[RoleBinding(role="default", connection_id="c1", model="m")],
    )
    with pytest.raises(ValueError):
        await build_router_from_registry(reg)


async def test_bindings_without_default_is_refused() -> None:
    # A role bound but no `default` → any unbound role would KeyError mid-beat; refuse at build.
    reg = FakeRegistry(
        connections=[ModelConnection(id="c1", name="stub", provider="stub")],
        bindings=[RoleBinding(role="narrator", connection_id="c1", model="stub")],
    )
    with pytest.raises(ValueError):
        await build_router_from_registry(reg)


async def test_all_roles_bound_without_default_is_ok() -> None:
    # Every non-default role bound → no default needed → a valid router (no crash path).
    roles = ["narrator", "extractor", "planner", "embedder", "dialogue", "judge"]
    reg = FakeRegistry(
        connections=[ModelConnection(id="c1", name="stub", provider="stub")],
        bindings=[RoleBinding(role=r, connection_id="c1", model="stub") for r in roles],
    )
    router = await build_router_from_registry(reg)
    assert router is not None
    assert isinstance(router._provider_for("narrator"), StubProvider)
