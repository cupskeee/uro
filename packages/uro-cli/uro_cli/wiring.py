"""Config resolution + adapter wiring (docs/04, 08, 14).

The CLI is where concrete adapters get instantiated and handed to the core — the
engine itself only ever sees ports. Roles can be bound to different providers/models
via uro.toml `[llm.roles]` (e.g. narrator = "anthropic:claude-sonnet-5",
extractor = "openai:gpt-4o-mini", embedder = "openai:text-embedding-3-small"); the
`--provider` flag is the default for any role not pinned there. Secrets stay in env
(references, never values), per docs/14.
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Any

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.providers.adapters.anthropic import AnthropicProvider
from uro_core.providers.adapters.openai_compat import OpenAICompatProvider
from uro_core.providers.adapters.stub import StubProvider
from uro_core.providers.base import LLMProvider
from uro_core.providers.router import ProviderRouter

DEFAULT_DSN = "postgresql://uro:uro@localhost:5433/uro"

logger = logging.getLogger(__name__)


def db_dsn() -> str:
    return os.environ.get("URO_DATABASE_URL", DEFAULT_DSN)


def build_store() -> PostgresEventStore:
    return PostgresEventStore(db_dsn())


def _config() -> dict[str, Any]:
    explicit = os.environ.get("URO_CONFIG")
    path = Path(explicit) if explicit else Path("uro.toml")
    if not path.is_file():
        if explicit:  # explicitly set but not a file → loud, not silently ignored
            raise RuntimeError(f"URO_CONFIG={path} is not a file")
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"malformed config {path}: {exc}") from exc


def load_role_specs() -> dict[str, str]:
    roles = _config().get("llm", {}).get("roles", {})
    return {str(role): str(spec) for role, spec in roles.items()}


def build_provider(kind: str, model: str | None) -> LLMProvider:
    if kind == "stub":
        return StubProvider()
    if kind == "local":
        return OpenAICompatProvider(
            model=model or "llama3.1",
            base_url=os.environ.get("URO_LOCAL_BASE_URL", "http://localhost:11434/v1"),
        )
    if kind == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        return OpenAICompatProvider(model=model or "gpt-4o-mini", api_key=api_key)
    if kind == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        return AnthropicProvider(model=model or "claude-sonnet-5", api_key=api_key)
    raise ValueError(
        f"unknown provider kind {kind!r} (expected: stub | local | openai | anthropic)"
    )


def provider_from_spec(spec: str) -> LLMProvider:
    """Parse a 'kind:model' (or bare 'kind') role binding into a provider."""
    kind, _, model = spec.partition(":")
    return build_provider(kind.strip(), model.strip() or None)


def build_embedder(kind: str) -> LLMProvider | None:
    """A provider bound to an EMBEDDING model for the `embedder` role, or None if the
    default already embeds (stub) / no embedding provider is available (anthropic w/o
    OpenAI key → semantic recall degrades gracefully to structured-only)."""
    if kind == "stub":
        return None
    if kind == "local":
        return OpenAICompatProvider(
            model=os.environ.get("URO_EMBED_MODEL", "nomic-embed-text"),
            base_url=os.environ.get("URO_LOCAL_BASE_URL", "http://localhost:11434/v1"),
        )
    if kind in ("openai", "anthropic"):
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return None  # no embedding endpoint → semantic recall disabled (best-effort)
        return OpenAICompatProvider(
            model=os.environ.get("URO_EMBED_MODEL", "text-embedding-3-small"), api_key=api_key
        )
    return None


def build_router(kind: str, model: str | None) -> ProviderRouter:
    # The default provider is required (it narrates + backs unpinned roles). A *pinned*
    # role whose provider can't be built (e.g. an unused role missing its key) is skipped
    # with a warning and falls back to the default, rather than bricking the whole router.
    default = build_provider(kind, model)
    bindings: dict[str, LLMProvider] = {}
    for role, spec in load_role_specs().items():
        try:
            bindings[role] = provider_from_spec(spec)
        except (RuntimeError, ValueError) as exc:
            logger.warning("role %r (%s) unavailable, using default: %s", role, spec, exc)
    if "embedder" not in bindings:
        try:
            embedder = build_embedder(kind)
        except (RuntimeError, ValueError):
            embedder = None
        if embedder is not None:
            bindings["embedder"] = embedder
    return ProviderRouter(bindings=bindings, default=default)
