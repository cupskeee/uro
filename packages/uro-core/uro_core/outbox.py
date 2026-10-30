"""The outbox relay (D-57, docs/07): moves committed beats from the store's outbox to a publisher.

The store writes one outbox row per commit in the commit's own transaction; `relay_outbox` drains
pending rows through any `OutboxPublisher` — `adapters.nats.NatsJetStreamPublisher` to reach other
processes, or `InProcessPublisher` for subscribers in this one. Delivery is at-least-once.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from uro_core.ports.outbox import OutboxMessage, OutboxPublisher, OutboxStore

Subscriber = Callable[[OutboxMessage], Awaitable[None]]


async def relay_outbox(store: OutboxStore, publisher: OutboxPublisher, *, limit: int = 100) -> int:
    """Publish every pending commit, a batch of `limit` at a time; returns how many went out. A
    publish failure raises after rolling its batch back to pending — the next call retries it."""
    total = 0
    while True:
        sent = await store.drain_outbox(publisher.publish, limit=limit)
        total += sent
        if sent < limit:
            return total


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
