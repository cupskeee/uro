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


def build_router(kind: str, model: str | None) -> ProviderRouter:
    # Phase 0: one provider serves every role via the router's default binding.
    return ProviderRouter(bindings={}, default=build_provider(kind, model))
