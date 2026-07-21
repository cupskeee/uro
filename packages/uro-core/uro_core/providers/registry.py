"""Build providers/routers from model-connection registry rows + model discovery (D-47).

Lives in the adapter layer (`uro_core.providers`, NOT the core ring): it constructs concrete
provider adapters and talks to Postgres/HTTP, so — like `uro_core.adapters` — the import-linter
forbids `domain/timeline/engines/pipeline/memory` from importing it. Shared by the CLI wiring
(`serve` router resolution) and the server (`refresh`/`test`/`reload` endpoints), so it must not
live in `uro_cli`.
"""

from __future__ import annotations

import logging
import os

import asyncpg
import httpx

from uro_core.ports.model_registry import ModelConnection, ModelRegistry
from uro_core.providers.adapters.anthropic import AnthropicProvider
from uro_core.providers.adapters.openai_compat import OpenAICompatProvider
from uro_core.providers.adapters.stub import StubProvider
from uro_core.providers.base import LLMProvider
from uro_core.providers.router import ProviderRouter

logger = logging.getLogger(__name__)


def provider_from_connection(
    conn: ModelConnection, model: str, access_token: str | None
) -> LLMProvider:
    """Construct a provider from a registry connection row + its DECRYPTED credential (D-47). Pure
    — the caller resolves the secret (via the store's `get_secret`), so this is reusable for the
    router, the `test` probe, and `refresh` alike."""
    kind = conn.provider
    if kind == "stub":
        return StubProvider()
    if kind == "openai":
        return OpenAICompatProvider(
            model=model, api_key=access_token, base_url=conn.base_url or "https://api.openai.com/v1"
        )
    if kind == "local":
        return OpenAICompatProvider(
            model=model,
            api_key=access_token,
            base_url=conn.base_url
            or os.environ.get("URO_LOCAL_BASE_URL", "http://localhost:11434/v1"),
        )
    if kind == "openai_compat":
        if not conn.base_url:
            raise ValueError(f"connection {conn.name!r} (openai_compat) requires a base_url")
        return OpenAICompatProvider(model=model, api_key=access_token, base_url=conn.base_url)
    if kind == "anthropic":
        return AnthropicProvider(
            model=model,
            api_key=access_token or "",
            base_url=conn.base_url or "https://api.anthropic.com",
        )
    raise ValueError(f"connection {conn.name!r} has unknown provider kind {kind!r}")


async def _access_token(store: ModelRegistry, conn: ModelConnection) -> str | None:
    if conn.auth_id is None:
        return None
    secret = await store.get_secret(conn.auth_id)
    return secret[0] if secret is not None else None


async def build_router_from_registry(store: ModelRegistry) -> ProviderRouter | None:
    """Build a `ProviderRouter` from the DB-backed model-connection registry (D-47, docs/20).

    Returns None when the registry has no usable bindings (empty, or its tables are not migrated
    yet) so the caller falls back to the `uro.toml`/`--provider` seed. The reserved `default` role
    becomes the router default; a binding to a missing/disabled connection is skipped (that role
    then falls back to `default`). A CONFIGURED binding whose provider can't be built (bad KEK,
    missing credential) raises — the operator asked for it, so failing loudly beats silently
    serving the stub.
    """
    try:
        role_bindings = await store.list_role_bindings()
    except asyncpg.UndefinedTableError:
        return None  # registry tables absent (pre-D47 / unmigrated DB) → use the seed
    if not role_bindings:
        return None
    connections = {c.id: c for c in await store.list_connections()}
    bindings: dict[str, LLMProvider] = {}
    default: LLMProvider | None = None
    for rb in role_bindings:
        conn = connections.get(rb.connection_id)
        if conn is None or not conn.is_enabled:
            logger.warning(
                "role %r → connection %r missing/disabled; falling back to default",
                rb.role,
                rb.connection_id,
            )
            continue
        provider = provider_from_connection(conn, rb.model, await _access_token(store, conn))
        if rb.role == "default":
            default = provider
        else:
            bindings[rb.role] = provider
    if default is None and not bindings:
        return None
    return ProviderRouter(bindings=bindings, default=default)


# ---- Model discovery + modality classification (slice 3) ------------------------


# The `embedder` role needs an EMBEDDING model; other roles a chat model. There is no universal
# cross-provider "is_embedding" flag, so classify per adapter by the model id. Unknown providers →
# "unknown" (the binding is allowed with a warning; a live `test` is the definitive check).
def classify_modality(provider: str, model_id: str) -> str:
    m = model_id.lower()
    if provider == "anthropic":
        return "chat"  # Anthropic ships no embedding models
    if provider in ("openai", "openai_compat", "local", "stub"):
        return "embedding" if "embed" in m else "chat"
    return "unknown"


async def discover_models(
    conn: ModelConnection,
    access_token: str | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, str]]:
    """List a connection's available models as `[{id, modality}]` (best-effort). `stub` returns a
    canned pair; the real kinds hit the provider's model-list endpoint (OpenAI-compatible `/models`,
    Anthropic `/v1/models`). A network/HTTP failure raises (the caller maps it to a clean error).
    `transport` is a test seam."""
    if conn.provider == "stub":
        return [
            {"id": "stub-chat", "modality": "chat"},
            {"id": "stub-embed", "modality": "embedding"},
        ]
    headers: dict[str, str] = {}
    if conn.provider == "anthropic":
        base = (conn.base_url or "https://api.anthropic.com").rstrip("/")
        url = f"{base}/v1/models"
        if access_token:
            headers["x-api-key"] = access_token
            headers["anthropic-version"] = "2023-06-01"
    else:  # openai | openai_compat | local — the OpenAI-compatible /models endpoint
        base = (conn.base_url or "https://api.openai.com/v1").rstrip("/")
        url = f"{base}/models"
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
    async with httpx.AsyncClient(timeout=30.0, transport=transport) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    ids = sorted({d["id"] for d in data if isinstance(d, dict) and isinstance(d.get("id"), str)})
    return [{"id": i, "modality": classify_modality(conn.provider, i)} for i in ids]
