"""EventStore port (docs/01, 07).

The core ring (engines, pipeline) depends only on this Protocol — never on the
Postgres adapter that implements it. Phase 0 surface: create worlds/campaigns,
append a beat commit, and read recent beats back from the log (the resume path).
"""

from __future__ import annotations

from typing import Any, Protocol

from uro_core.domain.events import BeatResolvedPayload, DomainEvent
from uro_core.metering import LLMCall
from uro_core.timeline.models import (
    Branch,
    BranchInfo,
    Campaign,
    Commit,
    LineageEntry,
    Marker,
    World,
)


class EventStore(Protocol):
    async def create_world(self, name: str) -> World:
        """Create a world: its `main` branch and a `WorldGenesis` genesis commit."""
        ...

    async def get_world_by_name(self, name: str) -> World | None: ...

    async def get_world(self, world_id: str) -> World | None: ...

    async def create_campaign(self, world_id: str, branch_id: str) -> Campaign: ...

    async def get_campaign(self, campaign_id: str) -> Campaign | None: ...

    async def start_campaign(
        self,
        world_id: str,
        branch_id: str,
        *,
        participant_id: str,
        adopt_actor_id: str | None = None,
        new_pc_name: str | None = None,
        new_pc_id: str | None = None,
        pc_sheet: dict[str, Any] | None = None,
        ruleset_id: str = "",
        seed: int = 0,
    ) -> Campaign:
        """Create a campaign on a branch and bind its PC (adopt an existing actor or make
        a fresh one), optionally with a ruleset-built character sheet — CampaignStarted +
        PCBound (+ SheetUpdated) as events (docs/03, 06, 12)."""
        ...

    async def end_campaign(
        self, campaign_id: str, marker_name: str, *, outcome: str = ""
    ) -> Marker:
        """End a campaign: release its PCs to NPCs, mark + snapshot the closing commit."""
        ...

    async def time_skip(
        self, branch_id: str, days: int, *, reason: str = "time-skip on fork"
    ) -> Commit:
        """Advance in-fiction time on a branch (TimeAdvanced + AdaptationApplied header)."""
        ...

    async def current_world_time(self, branch_id: str) -> int: ...

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

    # --- branching (docs/03): markers, fork-from-any-commit, lineage ---

    async def get_branch(self, branch_id: str) -> BranchInfo | None: ...

    async def get_branch_by_name(self, world_id: str, name: str) -> BranchInfo | None: ...

    async def list_branches(self, world_id: str) -> list[BranchInfo]: ...

    async def create_marker(self, world_id: str, name: str, branch_id: str) -> Marker:
        """Name a branch's current head (docs/03) and snapshot it as a fork root."""
        ...

    async def list_markers(self, world_id: str) -> list[Marker]: ...

    async def resolve_ref(self, world_id: str, ref: str) -> str:
        """A marker name or a raw commit_id → a commit_id (markers win on collision)."""
        ...

    async def fork_branch(self, world_id: str, from_ref: str, name: str) -> Branch:
        """Branch from any commit: a new ref + copy-on-fork projections materialized
        at `from_ref`, sharing history up to that point (docs/03)."""
        ...

    async def lineage(self, branch_id: str, limit: int = 50) -> list[LineageEntry]:
        """A branch's commit lineage head→genesis — the `uro log` view."""
        ...
