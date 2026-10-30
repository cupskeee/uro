"""The outbox relay (D-57, docs/07): moves committed beats from the store's outbox to a publisher.

The store writes one outbox row per commit in the commit's own transaction; `relay_outbox` drains
pending rows through any `OutboxPublisher` — `adapters.nats.NatsJetStreamPublisher` to reach other
processes, or `InProcessPublisher` for subscribers in this one. Delivery is at-least-once.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from uro_core.ports.outbox import OutboxMessage, OutboxPublisher, OutboxStore

Subscriber = Callable[[OutboxMessage], Awaitable[None]]

logger = logging.getLogger(__name__)

_MAX_BACKOFF = 30.0  # seconds — the ceiling a failing relay backs off to between retries


async def relay_outbox(store: OutboxStore, publisher: OutboxPublisher, *, limit: int = 100) -> int:
    """Publish every pending commit, a batch of `limit` at a time; returns how many went out. A
    publish failure raises after rolling its batch back to pending — the next call retries it."""
    if limit < 1:  # a zero batch never drains and never satisfies `sent < limit` — a hot loop
        raise ValueError(f"limit must be >= 1, got {limit}")
    total = 0
    while True:
        sent = await store.drain_outbox(publisher.publish, limit=limit)
        total += sent
        if sent < limit:
            return total


async def relay_forever(
    store: OutboxStore,
    publisher: OutboxPublisher,
    *,
    interval: float = 1.0,
    limit: int = 100,
    stop: asyncio.Event | None = None,
    on_published: Callable[[int], None] | None = None,
) -> None:
    """The relay DAEMON loop: drain, sleep `interval`, repeat — until `stop` is set. A publish error
    never ends it (a broker outage or one bad message must not take the relay down for every world):
    the batch stays pending, the error is logged, and the loop backs off, doubling up to
    `_MAX_BACKOFF`, then retries. The NATS client reconnects on its own."""
    backoff = interval
    while stop is None or not stop.is_set():
        try:
            sent = await relay_outbox(store, publisher, limit=limit)
        except ValueError:
            raise  # a bad `limit` is a caller bug, not an outage
        except Exception as exc:
            logger.warning("outbox relay: publish failed, retrying in %.1fs: %s", backoff, exc)
            await asyncio.sleep(backoff)
            backoff = min(_MAX_BACKOFF, backoff * 2)
            continue
        backoff = interval
        if sent and on_published is not None:
            on_published(sent)
        await asyncio.sleep(interval)


class InProcessPublisher:
    """Fan each published commit out to subscribers in THIS process, in order — the in-process
    event bus of docs/07 in miniature, and the publisher tests use. A subscriber that raises fails
    the publish, so the commit stays pending (at-least-once, like any publisher)."""

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.append(subscriber)

    async def publish(self, message: OutboxMessage) -> None:
        for subscriber in list(self._subscribers):
            await subscriber(message)
