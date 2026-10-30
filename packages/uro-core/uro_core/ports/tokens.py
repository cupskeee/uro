"""SessionTokenStore port (docs/18 B10, D-39).

Runtime session-token management: a durable, hashed, revocable registry so a player added to a
RUNNING server can authenticate without a restart (the `--token` argv map is frozen at launch).

Deliberately OFF THE BRANCH AXIS (like `ParticipantMemory`): a token authenticates a human to the
server independent of which what-if branch they explore, so it is NOT a projection, NOT event-
sourced, and never touched by `fork_branch`/`import_world` (fork-immunity is structural). Only
`sha256(token)` is persisted — the plaintext is minted once and never stored. The server layers an
in-process write-through cache over this so `resolve_participant` stays a sync, fast lookup.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel


class TokenInfo(BaseModel):
    """One runtime token's metadata — never the token, and never its full hash."""

    token_id: str  # the first 12 hex characters of sha256(token): a stable, non-secret handle
    participant_id: str
    created_at: datetime
    revoked: bool


class SessionTokenStore(Protocol):
    async def mint_token(self, token_hash: str, participant_id: str, campaign_id: str) -> None:
        """Persist a token (the caller passes the sha256 HASH — plaintext never reaches the store),
        binding it to a participant on a campaign. Upsert: re-minting the same hash is idempotent.
        """
        ...

    async def revoke_token(self, token_hash: str) -> bool:
        """Mark a token revoked (by hash). Returns True iff a live token was actually revoked."""
        ...

    async def list_session_tokens(self) -> list[tuple[str, str, str]]:
        """All non-revoked (token_hash, participant_id, campaign_id) triples — for the server to
        hydrate its in-process resolve cache at startup (so runtime-minted, campaign-scoped tokens
        survive a restart)."""
        ...

    async def campaign_tokens(self, campaign_id: str) -> list[TokenInfo]:
        """Every runtime token minted for a campaign (live and revoked), oldest first."""
        ...

    async def revoke_participant_tokens(self, campaign_id: str, participant_id: str) -> list[str]:
        """Revoke every LIVE token a participant holds on a campaign — how an operator removes a
        player without holding their token. Returns the revoked hashes, so the server can evict
        them from its resolve cache."""
        ...
