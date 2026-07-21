"""ModelRegistry port (D-47, docs/20).

The instance-level, DB-backed provider registry: model connections, their (encrypted) credentials,
and the role→(connection, model) bindings the `ProviderRouter` is built from. Configured over the
uro-server API by uro-cli / uro-loom; resolved into a router at server startup (wiring layer).

Deliberately OFF THE BRANCH/EVENT AXIS (like `SessionTokenStore`): instance/deployment config, not
world state — NOT a projection, NOT event-sourced, never touched by `fork_branch`/`import_world`/
`export_world`. Credentials are stored encrypted (app-level, under an env KEK); the plaintext secret
is exposed only transiently to the wiring layer via `get_secret`, to construct a provider.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel

# The engine roles a connection can back (resolved by the ProviderRouter). `default` is the
# fallback for any unbound role. Single source of truth for the CLI + server validation.
ROLES = frozenset({"default", "narrator", "extractor", "planner", "embedder", "dialogue", "judge"})


class ProviderCredential(BaseModel):
    """Credential METADATA — the plaintext token is never carried on this model (secrets leave the
    store only via `get_secret`, decrypted, for provider construction)."""

    id: str
    provider: str
    auth_mode: str = "api_key"
    has_access_token: bool = False
    has_refresh_token: bool = False
    last_refresh: datetime | None = None


class ModelConnection(BaseModel):
    id: str
    name: str
    provider: str
    base_url: str | None = None
    auth_id: str | None = None
    is_enabled: bool = True
    cached_models: list[dict[str, str]] | None = (
        None  # [{"id":…, "modality":"chat|embedding|unknown"}]
    )


class RoleBinding(BaseModel):
    role: str  # default | narrator | extractor | planner | embedder | dialogue | judge
    connection_id: str
    model: str


class ModelRegistry(Protocol):
    # --- credentials (secrets encrypted at rest) ---
    async def add_credential(
        self,
        *,
        provider: str,
        access_token: str | None,
        refresh_token: str | None = None,
        auth_mode: str = "api_key",
    ) -> str:
        """Store a credential (tokens encrypted before persistence); returns its new id."""
        ...

    async def list_credentials(self) -> list[ProviderCredential]:
        """Credential metadata only — never the plaintext secrets."""
        ...

    async def delete_credential(self, credential_id: str) -> bool:
        """Delete a credential. Connections linked to it have `auth_id` set NULL (not deleted)."""
        ...

    async def get_secret(self, credential_id: str) -> tuple[str | None, str | None] | None:
        """The DECRYPTED (access_token, refresh_token) for provider construction, or None if the
        credential id is unknown. Wiring-layer use only — the secret is held transiently."""
        ...

    # --- connections ---
    async def add_connection(
        self,
        *,
        name: str,
        provider: str,
        base_url: str | None = None,
        auth_id: str | None = None,
    ) -> str:
        """Register a provider endpoint; returns its new id."""
        ...

    async def list_connections(self) -> list[ModelConnection]: ...

    async def get_connection(self, connection_id: str) -> ModelConnection | None: ...

    async def set_connection_enabled(self, connection_id: str, enabled: bool) -> bool: ...

    async def set_connection_models(self, connection_id: str, models: list[dict[str, str]]) -> bool:
        """Replace a connection's discovered `cached_models` (D-47 slice 3 `refresh`)."""
        ...

    async def delete_connection(self, connection_id: str) -> bool:
        """Delete a connection; its role bindings cascade (those roles fall back to `default`)."""
        ...

    # --- role bindings ---
    async def set_role_binding(self, role: str, connection_id: str, model: str) -> None:
        """Bind an engine role to a connection+model (upsert). `default` is the router fallback."""
        ...

    async def list_role_bindings(self) -> list[RoleBinding]: ...

    async def delete_role_binding(self, role: str) -> bool: ...
