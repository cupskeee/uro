"""ParticipantMemory port (docs/18 B8, D-36).

A player's out-of-world meta-knowledge — knowledge that belongs to the PARTICIPANT, not the branch,
so it survives a fork/reset (the time-loop / roguelike / NG+ need). It is deliberately NOT an event
and NOT a projection, and because `fork_branch` only copies branch-keyed rows the lane is
fork-immune for free. Canon-safety is STRUCTURAL FOR DIRECT WIRING — nothing reads a note into
proj_claims,
proj_beliefs, the extractor, the planner, or belief-propagation. It surfaces only in the narrator
prompt as the player's private recollection; a note the narrator ECHOES into prose could be
re-extracted like any narrator output — that residual is fenced by narrator-tier trust (by-policy),
the same fence every narrator input already has (docs/13), not an additional hole.
"""

from __future__ import annotations

from typing import Protocol

from uro_core.timeline.models import ParticipantNote


class ParticipantMemory(Protocol):
    async def participant_remember(
        self,
        participant_id: str,
        world_ref: str,
        text: str,
        *,
        key: str | None = None,
        pinned: bool = False,
        entity_refs: list[str] | None = None,
    ) -> str:
        """Record (upsert) a note for a participant, scoped to a world. `key` dedups across loops —
        the same key overwrites (last-writer-wins); when omitted it is `sha256(text)` so identical
        prose still dedups and replay/retry stays idempotent. Returns the key used. Not
        branch-scoped and never copied by a fork."""
        ...

    async def participant_notes(self, participant_id: str, world_ref: str) -> list[ParticipantNote]:
        """All of a participant's notes for a world (ordered by key), for recall filtering + the
        codex UI. Small by design (author-flagged, opt-in)."""
        ...
