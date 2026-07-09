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


def test_build_router_tolerates_an_unbuildable_role(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A pinned role whose key is missing must be skipped (fall back to default), not
    # crash the whole router — else an unused/optional role bricks play (review inc 4).
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = tmp_path / "uro.toml"
    cfg.write_text('[llm.roles]\nextractor = "openai:gpt-4o-mini"\n')
    monkeypatch.setenv("URO_CONFIG", str(cfg))
    router = build_router("stub", None)  # no OpenAI key → extractor binding skipped
    assert "extractor" not in router._bindings  # fell back to the default provider


def test_config_errors_loudly_on_a_missing_explicit_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("URO_CONFIG", "/no/such/uro.toml")
    with pytest.raises(RuntimeError):
        load_role_specs()


def test_parse_role_models_parses_and_rejects_malformed() -> None:
    from uro_cli.wiring import parse_role_models

    assert parse_role_models(["planner=openai:gpt-4o", "extractor=gpt-4o-mini"]) == {
        "planner": "openai:gpt-4o",
        "extractor": "gpt-4o-mini",
    }
    assert parse_role_models(None) == {}
    for bad in ["planner", "=gpt-4o", "planner="]:
        with pytest.raises(ValueError):
            parse_role_models([bad])


def test_cli_role_model_override_full_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    # --role-model narrator=local:llama3.1 binds that role to a distinct provider (no key needed).
    from uro_core.providers.adapters.openai_compat import OpenAICompatProvider

    router = build_router("stub", None, {"narrator": "local:llama3.1"})
    assert isinstance(router._bindings["narrator"], OpenAICompatProvider)  # override applied
    assert isinstance(router._default, StubProvider)  # other roles still use the default


def test_cli_role_model_bare_model_uses_default_kind() -> None:
    # A bare 'model' (no 'kind:') reuses the default provider kind — the common "cheap default,
    # strong planner" case: planner=<model> stays on the same provider.
    from uro_core.providers.adapters.openai_compat import OpenAICompatProvider

    router = build_router("local", None, {"planner": "some-big-model"})
    planner = router._bindings["planner"]
    assert isinstance(planner, OpenAICompatProvider) and planner._model == "some-big-model"


def test_cli_override_wins_over_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from uro_core.providers.adapters.openai_compat import OpenAICompatProvider

    cfg = tmp_path / "uro.toml"
    cfg.write_text('[llm.roles]\nnarrator = "stub"\n')
    monkeypatch.setenv("URO_CONFIG", str(cfg))
    router = build_router("stub", None, {"narrator": "local:llama3.1"})
    assert isinstance(router._bindings["narrator"], OpenAICompatProvider)  # CLI beats uro.toml


def test_cli_override_fails_loudly_unlike_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # A config role missing its key is skipped with a warning; an explicit CLI override is intent,
    # so it raises rather than silently falling back.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        build_router("stub", None, {"planner": "openai:gpt-4o"})
