"""ProjectionQueries port (docs/02, 07).

Read-side of the timeline: structured recall (docs/04) and the extractor's
contradiction check (docs/05, 13) query current state through this port, never
by touching projection tables directly. The Postgres store implements it
alongside EventStore for Phase 1's single store.
"""

from __future__ import annotations

from typing import Any, Protocol

from uro_core.ports.event_store import EventStore
from uro_core.ports.vector import VectorIndex
from uro_core.timeline.models import (
    ActorView,
    BeliefView,
    ClaimView,
    EdgeView,
    FactionView,
    PlaceView,
    ThreadView,
)


class ProjectionQueries(Protocol):
    async def get_actor(self, branch_id: str, actor_id: str) -> ActorView | None: ...

    async def get_place(self, branch_id: str, place_id: str) -> PlaceView | None: ...

    async def list_places(self, branch_id: str) -> list[PlaceView]: ...

    async def list_factions(self, branch_id: str) -> list[FactionView]: ...

    async def get_faction(self, branch_id: str, faction_id: str) -> FactionView | None: ...

    async def list_edges(self, branch_id: str, rel_type: str | None = None) -> list[EdgeView]: ...

    async def edges_from(self, branch_id: str, src: str) -> list[EdgeView]: ...

    async def list_threads(self, branch_id: str) -> list[ThreadView]: ...

    async def world_style(self, branch_id: str) -> tuple[str, dict[str, str]]:
        """The narrator style (tone joined) + prompt-template overrides for a branch's world
        (docs/09), from its WorldGenesis. ('', {}) for a world created without a pack."""
        ...

    async def get_sheet(self, branch_id: str, actor_id: str) -> dict[str, Any] | None:
        """An actor's ruleset character sheet as a raw dict (docs/06); None if unsheeted."""
        ...

    async def is_pc(self, branch_id: str, actor_id: str) -> bool:
        """Per-branch PC-ness (docs/02): the same actor is a PC on one fork, an NPC on
        a sibling — answered by PCBound/PCReleased history, never a global flag."""
        ...

    async def active_pcs(self, branch_id: str) -> list[str]: ...

    async def campaign_pc(self, campaign_id: str) -> str | None: ...

    async def items_owned_by(self, branch_id: str, owner_ref: str) -> list[str]: ...

    async def get_item(self, branch_id: str, item_id: str) -> dict[str, Any] | None: ...

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

    async def fact_consistency(self, branch_id: str) -> tuple[int, int]:
        """Thesis PROXY metric T2 (docs/10): (survived, total) narrator-asserted claims.

        A narrator claim that survives as `truth=true` counts; one downgraded to `unknown`
        (the extractor flagged it as contradicting a recalled claim) does not. This is a
        proxy, not verification: it only catches contradictions the extractor self-flagged
        against *recalled* state — not all narration-vs-ground-truth disagreement. Best
        read as a regression trend. A real cross-check pass is future work.
        """
        ...


class EngineStore(EventStore, ProjectionQueries, VectorIndex, Protocol):
    """The read+write surface the engine needs: timeline (EventStore) + projection
    queries + semantic memory (VectorIndex). The Postgres store satisfies it
    structurally; Phase 1 has one store."""
