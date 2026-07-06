"""CLI wiring: embedder role, multi-role provider specs, and role config."""

from pathlib import Path

import pytest
from uro_cli.wiring import build_embedder, build_router, load_role_specs, provider_from_spec
from uro_core.providers.adapters.anthropic import AnthropicProvider
from uro_core.providers.adapters.stub import StubProvider


def test_stub_needs_no_separate_embedder() -> None:
    assert build_embedder("stub") is None  # StubProvider.embed works


def test_real_providers_bind_a_separate_embedder() -> None:
    # Real providers distinguish chat vs embedding endpoints — a separate binding is
    # required so the embedder role does not POST a chat model to /embeddings (review 1.3).
    local = build_embedder("local")
    assert local is not None and "embedder" in build_router("local", None)._bindings


async def test_stub_router_embeds_via_default() -> None:
    router = build_router("stub", None)
    vectors = await router.embed("embedder", ["hello world"])
    assert len(vectors) == 1 and len(vectors[0]) == 256


def test_provider_from_spec_parses_kind_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert isinstance(provider_from_spec("stub"), StubProvider)
    anthropic = provider_from_spec("anthropic:claude-sonnet-5")
    assert isinstance(anthropic, AnthropicProvider)


def test_role_specs_load_from_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = tmp_path / "uro.toml"
    cfg.write_text('[llm.roles]\nnarrator = "anthropic:claude-sonnet-5"\nextractor = "stub"\n')
    monkeypatch.setenv("URO_CONFIG", str(cfg))
    assert load_role_specs() == {"narrator": "anthropic:claude-sonnet-5", "extractor": "stub"}


def test_role_config_binds_roles(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    cfg = tmp_path / "uro.toml"
    cfg.write_text('[llm.roles]\nnarrator = "anthropic:claude-sonnet-5"\n')
    monkeypatch.setenv("URO_CONFIG", str(cfg))
    router = build_router("stub", None)
    assert isinstance(router._bindings["narrator"], AnthropicProvider)  # role override applied
