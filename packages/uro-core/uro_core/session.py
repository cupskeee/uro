"""Sessions, participants, and beat arbitration (docs/08). Single-player now, multiplayer-shaped.

The engine and the timeline already carry a `participant_id` on every intent/beat/event
(single-player is the degenerate participant-list-of-one). This module adds the two structural
seams docs/08 calls for so multiplayer is a new *implementation*, not a rewrite:

- `TurnArbiter` — beat admission. MVP `SoloArbiter` always admits; a `PartyArbiter` (free-roam
  proposal window / consensus, OQ-7) is a later arbiter behind the same port.
- `Participant` / `Session` — a session is a live connection context over a persistent campaign;
  `session(campaign_id, participants[])`. MVP: one session per campaign, one participant.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field


class Participant(BaseModel):
    participant_id: str
    actor_id: str | None = None  # the PC this participant drives (None until bound to one)


class Session(BaseModel):
    """A live connection context over a persistent campaign (docs/08). Not the story itself."""

    campaign_id: str
    participants: list[Participant] = Field(default_factory=list)


class TurnArbiter(Protocol):
    """Decides whether a participant's intent is admitted as the next beat (docs/08)."""

    async def admit(self, campaign_id: str, participant_id: str, intent: str) -> bool: ...


class SoloArbiter:
    """MVP arbiter: always admit. Single-player is one participant; nothing here assumes it —
    a `PartyArbiter` (consensus in free-roam; initiative order already arbitrates in encounters,
    OQ-7) is a different arbiter behind the same port, not a rewrite of the beat loop."""

    async def admit(self, campaign_id: str, participant_id: str, intent: str) -> bool:
        return True
