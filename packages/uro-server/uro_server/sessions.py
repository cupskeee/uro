"""Broadcast-shaped session fan-out (docs/08). Server→client messages address a SESSION and
fan out to all its live connections; with one connection this is invisible, with four it is
already correct. This is the multiplayer seam — no per-participant logic, just fan-out.
"""

from __future__ import annotations

import asyncio
from typing import Any


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
