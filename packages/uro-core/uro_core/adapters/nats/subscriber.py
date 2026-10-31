"""Live commits from NATS (D-67): what the outbox relay publishes, for a server to push to its
WebSocket clients.

A core subscription on `<prefix>.>` — the subjects the relay publishes to (JetStream captures them
for durable consumers; a core subscriber receives them as they're published). That makes it LIVE,
not durable: a server that is down misses what passed meanwhile, and its clients refetch state on
reconnect. A message that isn't a valid `OutboxMessage` is skipped with a warning, never fatal.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import nats
from nats.aio.client import Client
from nats.aio.subscription import Subscription
from pydantic import ValidationError

from uro_core.ports.outbox import OutboxMessage

logger = logging.getLogger(__name__)


class NatsCommitFeed:
    def __init__(self, url: str, *, subject_prefix: str = "uro") -> None:
        self._url = url
        self._prefix = subject_prefix
        self._nc: Client | None = None
        self._sub: Subscription | None = None

    async def connect(self) -> None:
        # keep reconnecting through a NATS outage instead of giving up after the client's default
        # ~2 minutes; the server's consume loop also reconnects if the feed ever ends (review)
        self._nc = await nats.connect(self._url, max_reconnect_attempts=-1)
        self._sub = await self._nc.subscribe(f"{self._prefix}.>")

    def __aiter__(self) -> AsyncIterator[OutboxMessage]:
        return self._messages()

    async def _messages(self) -> AsyncIterator[OutboxMessage]:
        if self._sub is None:
            raise RuntimeError("connect() first")
        async for msg in self._sub.messages:
            try:
                yield OutboxMessage.model_validate_json(msg.data)
            except ValidationError as exc:
                logger.warning("skipping a message on %s that isn't a commit: %s", msg.subject, exc)

    async def close(self) -> None:
        if self._nc is not None:
            await self._nc.drain()
            self._nc = None
