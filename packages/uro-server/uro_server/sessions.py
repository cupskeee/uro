"""Broadcast-shaped session fan-out (docs/08). Server→client messages address a SESSION and
fan out to all its live connections; with one connection this is invisible, with four it is
already correct. This is the multiplayer seam — no per-participant logic, just fan-out.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from typing import Any

from uro_core.ports.tokens import SessionTokenStore


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenRegistry:
    """Runtime token auth (docs/18 B10, D-39) behind the server's single `resolve_participant` choke
    point. Tiers (kept DISTINCT — D-39 review): `--token` argv tokens are ordinary PLAYER creds
    (server-wide, frozen at launch); a SUBSET flagged `--admin-token` is the OPERATOR tier that may
    seat/mint/revoke for ANOTHER participant (an ordinary player may only act for THEMSELVES — else
    every launch peer could impersonate any other). Runtime-minted player tokens are DURABLE (only
    `sha256(token)` in `session_tokens`), CAMPAIGN-SCOPED (a minted token is valid only on the
    campaign it was minted for — no cross-campaign hijack), and cached in-process so `resolve` stays
    a sync lookup. The cache re-hydrates from the store at startup, so a minted token survives a
    restart (an in-process-only registry would lock out every runtime-added player on restart)."""

    def __init__(
        self,
        store: SessionTokenStore | None,
        static_tokens: dict[str, str],
        admin_tokens: set[str] | None = None,
    ) -> None:
        self._store = store
        self._static = dict(static_tokens)  # plaintext token → participant (server-wide players)
        self._admin = set(admin_tokens or ())  # the OPERATOR subset (may act for others)
        self._cache: dict[str, tuple[str, str]] = {}  # sha256 → (participant, campaign) [minted]

    async def hydrate(self) -> None:
        """Load non-revoked runtime tokens from the store into the resolve cache (call once at
        startup, AFTER the store connects)."""
        if self._store is not None:
            for token_hash, participant_id, campaign_id in await self._store.list_session_tokens():
                self._cache[token_hash] = (participant_id, campaign_id)

    def resolve(self, token: str) -> str | None:
        """token → participant_id (or None). Sync: static by plaintext, minted by hash."""
        if not token:
            return None
        if token in self._static:
            return self._static[token]
        row = self._cache.get(_hash(token))
        return row[0] if row is not None else None

    def campaign_of(self, token: str) -> str | None:
        """The campaign a MINTED token is scoped to; None for a static/legacy (server-wide) or an
        unknown token — the caller only rejects a minted token used on the WRONG campaign."""
        if not token or token in self._static:
            return None
        row = self._cache.get(_hash(token))
        return row[1] if row is not None else None

    def is_admin(self, token: str) -> bool:
        """Operator tier only (D-39 review): the `--admin-token` subset, NOT every `--token` peer
        and never a runtime-minted player token. An operator may act for another participant."""
        return bool(token) and token in self._admin

    async def mint(self, participant_id: str, campaign_id: str) -> str:
        """Mint a fresh durable, campaign-scoped token; returns the plaintext ONCE (only its sha256
        is stored). Write the store first, then the cache (a crash re-hydrates from the store)."""
        if self._store is None:
            raise RuntimeError("token minting needs a store-backed server")
        token = secrets.token_urlsafe(32)
        token_hash = _hash(token)
        await self._store.mint_token(token_hash, participant_id, campaign_id)
        self._cache[token_hash] = (participant_id, campaign_id)
        return token

    async def revoke(self, token: str) -> bool:
        """Revoke a token (by its plaintext); True iff a live token was actually revoked. Evicts the
        cache so a NEW connect is denied immediately (a live socket survives — auth is checked once
        before accept; the per-message re-check is a named residual, D-39)."""
        if self._store is None:
            return False
        token_hash = _hash(token)
        ok = await self._store.revoke_token(token_hash)
        self._cache.pop(token_hash, None)
        return ok


class SessionHub:
    """Per-campaign pub/sub over live connections. Each connection subscribes a queue; a beat's
    messages are published to the campaign and land on every subscriber's queue."""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}

    def subscribe(self, campaign_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subs.setdefault(campaign_id, set()).add(queue)
        return queue

    def unsubscribe(self, campaign_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        subs = self._subs.get(campaign_id)
        if subs is not None:
            subs.discard(queue)
            if not subs:
                del self._subs[campaign_id]

    async def publish(self, campaign_id: str, message: dict[str, Any]) -> None:
        for queue in list(self._subs.get(campaign_id, ())):
            queue.put_nowait(message)

    def connections(self, campaign_id: str) -> int:
        return len(self._subs.get(campaign_id, ()))
