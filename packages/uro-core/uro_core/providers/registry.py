"""Build providers/routers from model-connection registry rows + model discovery (D-47).

Lives in the adapter layer (`uro_core.providers`, NOT the core ring): it constructs concrete
provider adapters and talks to Postgres/HTTP, so — like `uro_core.adapters` — the import-linter
forbids `domain/timeline/engines/pipeline/memory` from importing it. Shared by the CLI wiring
(`serve` router resolution) and the server (`refresh`/`test`/`reload` endpoints), so it must not
live in `uro_cli`.
"""

from __future__ import annotations

import asyncio
import logging
import os

import asyncpg
import httpx

from uro_core.errors import ProviderError
from uro_core.ports.model_registry import ROLES, ModelConnection, ModelRegistry
from uro_core.providers import codex_auth
from uro_core.providers.adapters.anthropic import AnthropicProvider
from uro_core.providers.adapters.codex import CodexResponsesProvider
from uro_core.providers.adapters.openai_compat import OpenAICompatProvider
from uro_core.providers.adapters.stub import StubProvider
from uro_core.providers.base import LLMProvider
from uro_core.providers.router import ProviderRouter

logger = logging.getLogger(__name__)

# Provider kinds that expose an embeddings endpoint. anthropic + codex serve only chat, so an
# `embedder` role resolving to one means semantic memory is OFF (embeddings raise every beat).
_EMBED_CAPABLE = frozenset({"openai", "local", "openai_compat", "stub"})


def _warn_if_embedder_cannot_embed(role_kind: dict[str, str]) -> None:
    """Announce whether semantic memory is on. The `embedder` role needs an embedding-capable
    provider; if it (directly, or via the `default` fallback) resolves to a chat-only kind, embeds
    fail silently every beat and long-range recall is dead — so warn LOUDLY at build (serve start /
    reload) rather than degrade quietly (review: the codex-embedder silent-off case)."""
    kind = role_kind.get("embedder") or role_kind.get("default")
    if kind is None:
        return  # nothing resolves the embedder yet (no default, no binding) — not our warning here
    if kind in _EMBED_CAPABLE:
        logger.info("semantic memory enabled (embedder → %s)", kind)
    else:
        logger.warning(
            "semantic memory is OFF: the 'embedder' role resolves to a %r provider, which has no "
            "embedding endpoint — long-range recall will silently do nothing. Bind embedder to an "
            "openai/local connection: `uro provider bind embedder <connection> <embedding-model>`.",
            kind,
        )


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
    if kind == "codex":
        # The PURE path (one-shot probe): a static token, NO auto-refresh. The router path uses
        # `build_codex_provider` (a refresh-capable CodexTokenSource) instead. An expired token here
        # just 401s → the probe reports "reconnect", which is the right signal.
        async def _static_token(force_refresh: bool = False) -> str:
            if access_token is None:
                raise ProviderError(f"codex connection {conn.name!r} has no credential — reconnect")
            return access_token

        return CodexResponsesProvider(
            model=model,
            token_provider=_static_token,
            base_url=conn.base_url or codex_auth.CODEX_BASE_URL,
        )
    raise ValueError(f"connection {conn.name!r} has unknown provider kind {kind!r}")


class CodexTokenSource:
    """A refresh-capable token callable for the ROUTER path: reads the connection's stored codex
    tokens and, on expiry (or a forced 401-retry), refreshes via the OAuth endpoint and PERSISTS the
    rotation. `build_router_from_registry` SHARES one instance across every role bound to the same
    credential (cached by `auth_id`), so its `asyncio.Lock` actually serializes concurrent beats —
    two roles on one ChatGPT subscription can't race a refresh of the same grant (review)."""

    def __init__(self, store: ModelRegistry, auth_id: str | None) -> None:
        self._store = store
        self._auth_id = auth_id
        self._lock = asyncio.Lock()

    async def __call__(self, force_refresh: bool = False) -> str:
        if self._auth_id is None:
            raise ProviderError("codex connection has no linked credential — reconnect")
        async with self._lock:
            secret = await self._store.get_secret(self._auth_id)
            access = secret[0] if secret is not None else None
            refresh = secret[1] if secret is not None else None
            if access is None:
                raise ProviderError("codex credential is missing its token — reconnect")
            if force_refresh or codex_auth.token_is_expiring(access):
                if refresh is None:
                    raise ProviderError("codex token expired and no refresh token — reconnect")
                data = await codex_auth.refresh_access_token(refresh)
                access = str(data["access_token"])
                await self._store.update_credential_tokens(
                    self._auth_id,
                    access_token=access,
                    refresh_token=str(data.get("refresh_token") or refresh),
                )
            return access


def build_codex_provider(
    store: ModelRegistry,
    conn: ModelConnection,
    model: str,
    *,
    source: CodexTokenSource | None = None,
) -> CodexResponsesProvider:
    """Router-path codex provider. Pass a SHARED `source` so all roles on one credential reuse a
    single token source + lock (review); omitted → a fresh (unshared) one for a lone binding."""
    return CodexResponsesProvider(
        model=model,
        token_provider=source or CodexTokenSource(store, conn.auth_id),
        base_url=conn.base_url or codex_auth.CODEX_BASE_URL,
    )


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
    role_kind: dict[str, str] = {}  # role → its connection's provider kind (for the embedder guard)
    # One CodexTokenSource per credential, SHARED across every role bound to it (review): the lock
    # only serializes refreshes if the roles hold the SAME instance.
    codex_sources: dict[str, CodexTokenSource] = {}
    for rb in role_bindings:
        conn = connections.get(rb.connection_id)
        if conn is None or not conn.is_enabled:
            logger.warning(
                "role %r → connection %r missing/disabled; falling back to default",
                rb.role,
                rb.connection_id,
            )
            continue
        provider: LLMProvider
        if conn.provider == "codex":
            key = conn.auth_id or conn.id
            source = codex_sources.setdefault(key, CodexTokenSource(store, conn.auth_id))
            provider = build_codex_provider(store, conn, rb.model, source=source)
        else:
            provider = provider_from_connection(conn, rb.model, await _access_token(store, conn))
        role_kind[rb.role] = conn.provider
        if rb.role == "default":
            default = provider
        else:
            bindings[rb.role] = provider
    _warn_if_embedder_cannot_embed(role_kind)
    if default is None:
        if not bindings:
            return None  # truly empty (or all bindings skipped) → seed fallback
        # Bindings but no `default`: any UNBOUND engine role would hit ProviderRouter._provider_for
        # → KeyError → the beat crashes mid-run (extractor/planner). Fail LOUD at build time (serve
        # startup / reload) with an actionable message rather than a latent per-beat crash
        # (holistic-review HIGH). If every role is bound, no default is needed and this is fine.
        unbound = sorted((ROLES - {"default"}) - set(bindings))
        if unbound:
            raise ValueError(
                f"model-connection registry binds {sorted(bindings)} but no 'default' role, and "
                f"roles {unbound} are unbound — they would crash mid-beat. Bind the default role: "
                "`uro provider bind default <connection> <model>`."
            )
    return ProviderRouter(bindings=bindings, default=default)


# ---- Model discovery + modality classification (slice 3) ------------------------


# The `embedder` role needs an EMBEDDING model; other roles a chat model. There is no universal
# cross-provider "is_embedding" flag, so classify per adapter by the model id. Unknown providers →
# "unknown" (the binding is allowed with a warning; a live `test` is the definitive check).
def classify_modality(provider: str, model_id: str) -> str:
    m = model_id.lower()
    if provider in ("anthropic", "codex"):
        return "chat"  # Anthropic ships no embedding models; the codex backend serves only chat
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
    if conn.provider == "codex":
        return await codex_auth.discover_codex_models(
            access_token or "",
            base_url=conn.base_url or codex_auth.CODEX_BASE_URL,
            transport=transport,
        )
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
