"""EventStore port (docs/01, 07).

The core ring (engines, pipeline) depends only on this Protocol — never on the
Postgres adapter that implements it. Phase 0 surface: create worlds/campaigns,
append a beat commit, and read recent beats back from the log (the resume path).
"""

from __future__ import annotations

from typing import Protocol

from uro_core.domain.events import BeatResolvedPayload, DomainEvent
from uro_core.metering import LLMCall
from uro_core.timeline.models import Campaign, Commit, World


class EventStore(Protocol):
    async def create_world(self, name: str) -> World:
        """Create a world: its `main` branch and a `WorldGenesis` genesis commit."""
        ...

    async def get_world_by_name(self, name: str) -> World | None: ...

    async def create_campaign(self, world_id: str, branch_id: str) -> Campaign: ...

    async def get_campaign(self, campaign_id: str) -> Campaign | None: ...

    async def append_beat(self, branch_id: str, events: list[DomainEvent]) -> Commit:
        """Append one beat commit to the branch head and advance it — one transaction."""
        ...

    async def recent_beats(self, branch_id: str, limit: int) -> list[BeatResolvedPayload]:
        """The last `limit` BeatResolved payloads on the branch, oldest-first.

        Reconstructed by walking the commit chain from the branch head — this is
        what makes a resumed session pick up from the log, not from process memory.
        """
        ...

    async def record_llm_call(self, call: LLMCall) -> None:
        """Record one stage-tagged LLM call for usage metering (docs/07, D-14)."""
        ...
