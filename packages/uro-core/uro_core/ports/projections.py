"""ProjectionQueries port (docs/02, 07).

Read-side of the timeline: structured recall (docs/04) and the extractor's
contradiction check (docs/05, 13) query current state through this port, never
by touching projection tables directly. The Postgres store implements it
alongside EventStore for Phase 1's single store.
"""

from __future__ import annotations

from typing import Protocol

from uro_core.ports.event_store import EventStore
from uro_core.ports.vector import VectorIndex
from uro_core.timeline.models import ActorView, BeliefView, ClaimView


class ProjectionQueries(Protocol):
    async def get_actor(self, branch_id: str, actor_id: str) -> ActorView | None: ...

    async def find_actor_by_name(self, branch_id: str, name: str) -> ActorView | None:
        """Match by name (case-insensitive) or alias — the seed of entity resolution."""
        ...

    async def list_actors(self, branch_id: str) -> list[ActorView]: ...

    async def get_claim(self, branch_id: str, claim_id: str) -> ClaimView | None: ...

    async def list_claims(self, branch_id: str) -> list[ClaimView]: ...

    async def claims_about(self, branch_id: str, entity_ref: str) -> list[ClaimView]:
        """Claims whose subject_refs include the entity — the spine of structured recall."""
        ...

    async def beliefs_of(self, branch_id: str, actor_id: str) -> list[BeliefView]: ...


class EngineStore(EventStore, ProjectionQueries, VectorIndex, Protocol):
    """The read+write surface the engine needs: timeline (EventStore) + projection
    queries + semantic memory (VectorIndex). The Postgres store satisfies it
    structurally; Phase 1 has one store."""
