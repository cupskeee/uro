"""The outbox PORT (D-57, docs/07): how committed beats leave the engine for other processes.

Every commit writes an outbox row in its own transaction (the store adapter's job); a relay drains
pending rows through an `OutboxPublisher` — NATS JetStream in `adapters/nats`, or an in-process
subscriber. Delivery is AT-LEAST-ONCE: a publish failure leaves the rows pending for the next drain,
so a consumer must tolerate a repeat (the NATS adapter dedups on `commit_id` server-side). The core
ring never imports a concrete publisher.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from pydantic import BaseModel


class OutboxMessage(BaseModel):
    """One committed beat, as published: its coordinates plus every event in commit order. The
    events are the full log entries — operator-level data (D-45), not a player-facing feed."""

    v: int = 1
    seq: int
    world_id: str
    branch_id: str
    commit_id: str
    parent_id: str | None
    depth: int
    events: list[dict[str, Any]]


class OutboxPublisher(Protocol):
    async def publish(self, message: OutboxMessage) -> None:
        """Deliver one message; raise to leave it (and the rest of the batch) pending."""
        ...


class OutboxStore(Protocol):
    async def outbox_pending(self) -> int:
        """How many committed beats are waiting to be published."""
        ...

    async def drain_outbox(
        self, deliver: Callable[[OutboxMessage], Awaitable[None]], *, limit: int = 100
    ) -> int:
        """Claim up to `limit` pending rows in `seq` order (skipping rows another relay holds),
        `deliver` each, then mark them published — all in one transaction, so a failure part-way
        leaves the whole batch pending. Returns how many were published."""
        ...


class CommitFeed(Protocol):
    """Live commits for a server to push to its clients (D-67) — e.g. what the outbox relay
    publishes to NATS. LIVE, not durable: a consumer that's down misses what passed meanwhile (its
    clients refetch state on reconnect); the outbox and the stream stay the durable record."""

    async def connect(self) -> None: ...

    def __aiter__(self) -> AsyncIterator[OutboxMessage]: ...

    async def close(self) -> None: ...
