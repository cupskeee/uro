"""Sessions, participants, and beat arbitration (docs/08, OQ-7 → D-31).

The engine and timeline carry a `participant_id` on every intent/beat/event, and the pipeline
resolves each beat as the acting participant's PC (7.1). This module is the ARBITRATION seam:
who gets to take the next free-roam beat when a party shares one campaign.

- `TurnArbiter` — beat admission + roster/rotation lifecycle. `SoloArbiter` always admits (the
  degenerate one-participant case). `PartyArbiter` implements ROUND-ROBIN turn ownership over the
  connected roster (D-31): only the turn-holder is admitted; the token rotates when a beat commits.
  Proposal-window / consensus arbiters are future implementations behind the SAME port (the
  `AdmitDecision.QUEUED` value is reserved for a proposal window that holds an intent).
- Turn state is SESSION-ONLY (in the arbiter), not event-sourced (D-31): it is a live-connection
  concern, not campaign history — a reconnect re-forms the roster and restarts the round.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field


class AdmitDecision(StrEnum):
    """The verdict on a submitted intent (docs/08). A bool could not distinguish 'wait, not your
    turn' (retry when the token rotates) from 'rejected' (do not retry) — round-robin needs both."""

    ADMITTED = "admitted"  # run it now
    NOT_YOUR_TURN = "not_your_turn"  # valid, but another participant holds the turn — hold/retry
    REJECTED = "rejected"  # refused (e.g. invalid/vetoed) — do not retry
    QUEUED = "queued"  # reserved: a proposal-window arbiter holds it until the window closes


class Participant(BaseModel):
    participant_id: str
    actor_id: str | None = None  # the PC this participant drives (None until bound to one)


class Session(BaseModel):
    """A live connection context over a persistent campaign (docs/08). Not the story itself."""

    campaign_id: str
    participants: list[Participant] = Field(default_factory=list)


class TurnArbiter(Protocol):
    """Decides whether a participant's intent is admitted as the next beat, and tracks the live
    roster so a stateful arbiter can rotate turns (docs/08, OQ-7)."""

    async def admit(self, campaign_id: str, participant_id: str, intent: str) -> AdmitDecision: ...

    async def note_joined(self, campaign_id: str, participant_id: str) -> None:
        """A participant connected — add to the turn roster (for arbiters that track one)."""
        ...

    async def note_left(self, campaign_id: str, participant_id: str) -> None:
        """A participant disconnected — drop from the roster."""
        ...

    async def beat_committed(self, campaign_id: str, participant_id: str, beat_id: str) -> None:
        """A beat committed — the signal a stateful arbiter uses to advance the turn/close a
        window. SoloArbiter never needed this; its absence was the OQ-7 leak."""
        ...


class SoloArbiter:
    """MVP arbiter: always admit. Single-player is one participant; the roster/rotation hooks are
    no-ops (there is never contention). A `PartyArbiter` is a different arbiter behind the same
    port, not a rewrite of the beat loop."""

    async def admit(self, campaign_id: str, participant_id: str, intent: str) -> AdmitDecision:
        return AdmitDecision.ADMITTED

    async def note_joined(self, campaign_id: str, participant_id: str) -> None:
        return None

    async def note_left(self, campaign_id: str, participant_id: str) -> None:
        return None

    async def beat_committed(self, campaign_id: str, participant_id: str, beat_id: str) -> None:
        return None


class PartyArbiter:
    """Round-robin free-roam arbitration (OQ-7 → D-31). One turn token per campaign rotates over
    the connected roster in JOIN ORDER: only the current holder is ADMITTED; everyone else gets
    NOT_YOUR_TURN; the token advances to the next member when a beat commits. Deterministic (no
    clock/random) and session-only (the roster is who is connected now). A departing holder passes
    the token forward; an empty roster admits (degenerate). Proposal-window/consensus are future
    arbiters behind this same port (see AdmitDecision.QUEUED)."""

    def __init__(self) -> None:
        self._ring: dict[str, list[str]] = {}  # campaign_id → participants in join order
        self._turn: dict[str, int] = {}  # campaign_id → index of the current turn-holder
        # campaign_id → participant_id → live connection count. A participant is in the ring while
        # they hold ≥1 connection, so a second device / an overlapping reconnect closing one socket
        # does not drop a still-connected player from the round (cross-phase review P5xP7).
        self._conns: dict[str, dict[str, int]] = {}

    def _holder(self, campaign_id: str) -> str | None:
        ring = self._ring.get(campaign_id) or []
        if not ring:
            return None
        return ring[self._turn.get(campaign_id, 0) % len(ring)]

    async def admit(self, campaign_id: str, participant_id: str, intent: str) -> AdmitDecision:
        holder = self._holder(campaign_id)
        if holder is None or holder == participant_id:
            return AdmitDecision.ADMITTED
        return AdmitDecision.NOT_YOUR_TURN

    async def note_joined(self, campaign_id: str, participant_id: str) -> None:
        conns = self._conns.setdefault(campaign_id, {})
        conns[participant_id] = conns.get(participant_id, 0) + 1
        if conns[participant_id] == 1:  # first connection → enter the ring
            self._ring.setdefault(campaign_id, []).append(participant_id)

    async def note_left(self, campaign_id: str, participant_id: str) -> None:
        conns = self._conns.get(campaign_id, {})
        if participant_id in conns:
            conns[participant_id] -= 1
            if conns[participant_id] > 0:
                return  # still connected on another socket — keep them in the ring
            del conns[participant_id]
        ring = self._ring.get(campaign_id)
        if not ring or participant_id not in ring:
            return
        idx = ring.index(participant_id)
        ring.remove(participant_id)
        # Keep the token pointing at the SAME surviving holder: removing someone at/before the
        # cursor shifts everyone down by one, so decrement to compensate (clamped by the modulo).
        cur = self._turn.get(campaign_id, 0)
        if ring and idx <= cur % (len(ring) + 1):
            self._turn[campaign_id] = max(0, cur - 1)

    async def beat_committed(self, campaign_id: str, participant_id: str, beat_id: str) -> None:
        ring = self._ring.get(campaign_id)
        if ring:  # rotate the token to the next member
            self._turn[campaign_id] = (self._turn.get(campaign_id, 0) + 1) % len(ring)
