"""Config resolution + adapter wiring (docs/08, 14).

The CLI is where concrete adapters get instantiated and handed to the core — the
engine itself only ever sees ports. Phase 0 config comes from env + flags; full
uro.toml parsing arrives with the server (Phase 5).
"""

from __future__ import annotations

import os

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.providers.adapters.openai_compat import OpenAICompatProvider
from uro_core.providers.adapters.stub import StubProvider
from uro_core.providers.base import LLMProvider
from uro_core.providers.router import ProviderRouter

DEFAULT_DSN = "postgresql://uro:uro@localhost:5433/uro"


def db_dsn() -> str:
    return os.environ.get("URO_DATABASE_URL", DEFAULT_DSN)


def build_store() -> PostgresEventStore:
    return PostgresEventStore(db_dsn())


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
    raise ValueError(f"unknown provider kind {kind!r} (expected: stub | local | openai)")


def build_embedder(kind: str) -> LLMProvider | None:
    """A provider bound to an EMBEDDING model for the `embedder` role.

    Chat and embedding models are different endpoints; without this the embedder role
    would POST a chat model to /embeddings and 400. `None` means the default provider
    already embeds (the stub does). Embedding model is overridable via URO_EMBED_MODEL.
    """
    if kind == "stub":
        return None  # StubProvider.embed works
    if kind == "local":
        return OpenAICompatProvider(
            model=os.environ.get("URO_EMBED_MODEL", "nomic-embed-text"),
            base_url=os.environ.get("URO_LOCAL_BASE_URL", "http://localhost:11434/v1"),
        )
    if kind == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        return OpenAICompatProvider(
            model=os.environ.get("URO_EMBED_MODEL", "text-embedding-3-small"), api_key=api_key
        )
    raise ValueError(f"unknown provider kind {kind!r} (expected: stub | local | openai)")


def build_router(kind: str, model: str | None) -> ProviderRouter:
    # One provider serves the generative roles; the embedder role gets an embedding
    # model where the provider distinguishes them (real providers do; the stub doesn't).
    bindings: dict[str, LLMProvider] = {}
    embedder = build_embedder(kind)
    if embedder is not None:
        bindings["embedder"] = embedder
    return ProviderRouter(bindings=bindings, default=build_provider(kind, model))
