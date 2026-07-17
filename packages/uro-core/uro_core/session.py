"""Sessions, participants, and beat arbitration (docs/08, OQ-7 → D-31).

The engine and timeline carry a `participant_id` on every intent/beat/event, and the pipeline
resolves each beat as the acting participant's PC (7.1). This module is the ARBITRATION seam:
who gets to take the next free-roam beat when a party shares one campaign.

- `TurnArbiter` — beat admission + roster/rotation lifecycle. `SoloArbiter` always admits (the
  degenerate one-participant case). `PartyArbiter` implements ROUND-ROBIN turn ownership over the
  connected roster (D-31): only the turn-holder is admitted; the token rotates when a beat commits.
- `ProposalWindowArbiter` / `VoteArbiter` (D-38) are round-robin like `PartyArbiter` but add the
  propose-then-act (`AdmitDecision.QUEUED` now LIVE) and consensus/vote coordination shapes — all
  riding a NON-CANON coordination lane so no proposal/debate/vote burns a canonical beat. The
  shapes that would touch a load-bearing seam stay DEFERRED behind this same port (D-38): consensual
  PvP (edits the anti-grief invariant + effect path), simultaneous/composite beats (rewrites the
  one-intent-one-beat loop), and reactive/interrupt (needs the deferred per-campaign concurrency
  guard — an out-of-band admit would be a second concurrent writer).
- Turn state is SESSION-ONLY (in the arbiter), not event-sourced (D-31): it is a live-connection
  concern, not campaign history — a reconnect re-forms the roster and restarts the round. The
  coordination shapes are likewise session-only (proposals/votes are ephemeral coordination, not
  canon) — a disconnect drops them, by design.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

# NOTE (consolidation, 2026-07-09): the speculative `Session`/`Participant` Pydantic models that
# once lived here were REMOVED as dead code — nothing constructed them. The live per-campaign
# roster is tracked by the arbiter (PartyArbiter._ring) + the server's SessionHub; the acting
# participant's PC is resolved from proj_pcs via store.pc_for_participant (7.1), not a model here.


class AdmitDecision(StrEnum):
    """The verdict on a submitted intent (docs/08). A bool could not distinguish 'wait, not your
    turn' (retry when the token rotates) from 'rejected' (do not retry) — round-robin needs both."""

    ADMITTED = "admitted"  # run it now
    NOT_YOUR_TURN = "not_your_turn"  # valid, but another participant holds the turn — hold/retry
    REJECTED = "rejected"  # refused (e.g. invalid/vetoed) — do not retry
    QUEUED = "queued"  # a proposal-window arbiter HELD it as a proposal (D-38) — no beat, surfaced


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
        if not ring:
            self._turn.pop(campaign_id, None)
            return
        # Keep the token correct after removal (gap-report Seventh Vault G-17 — the old `idx <= cur`
        # form stepped the cursor BACKWARD when the HOLDER left, giving one player a double turn and
        # skipping the next). Only a member strictly BEFORE the holder shifts the holder down a
        # slot; when the holder THEMSELVES leaves (idx == cur) the successor slides into the
        # cursor's slot, so the token stays put and hands off. Wrap into the shrunk ring either way.
        cur = self._turn.get(campaign_id, 0)
        if idx < cur:
            cur -= 1
        self._turn[campaign_id] = cur % len(ring)

    async def beat_committed(self, campaign_id: str, participant_id: str, beat_id: str) -> None:
        ring = self._ring.get(campaign_id)
        if ring:  # rotate the token to the next member
            self._turn[campaign_id] = (self._turn.get(campaign_id, 0) + 1) % len(ring)


class ProposalWindowArbiter(PartyArbiter):
    """Propose-then-act turns (D-38, G-10). Round-robin exactly like `PartyArbiter` — the ring,
    rotation, roster refcount, and the G-17 departure fix are all inherited — with ONE change: a
    NON-HOLDER's intent is QUEUED (surfaced to the table as a proposal on the non-canon lane)
    instead of a silent NOT_YOUR_TURN. So the party can float 'we should…' as a first-class,
    visible proposal without burning a canonical beat; the holder still acts normally and the token
    rotates on commit. `admit` stays a PURE query (no state written here); window state, if any, is
    the server's broadcast — nothing is held server-side. Auto-promoting a winning proposal into an
    on-behalf-of beat (take_pending) is DEFERRED (D-38): the decided action is enacted as the
    holder's ordinary one-intent-one-beat. Session-only (D-31)."""

    async def admit(self, campaign_id: str, participant_id: str, intent: str) -> AdmitDecision:
        holder = self._holder(campaign_id)
        if holder is None or holder == participant_id:
            return AdmitDecision.ADMITTED
        return AdmitDecision.QUEUED  # not your turn, but surfaced as a proposal (not silently held)


@dataclass(frozen=True)
class VoteOutcome:
    """The result of casting one vote (D-38, G-11). `tally` is the current choice→count map;
    `decided` is the winning choice once the round resolves, else None (collecting / a tie)."""

    tally: dict[str, int]
    decided: str | None


@runtime_checkable
class VoteCoordinator(Protocol):
    """OPTIONAL arbiter capability (D-38, G-11): tally out-of-band votes on the non-canon lane. The
    server dispatches a `vote` frame here only when `isinstance(arb, VoteCoordinator)` — arbiters
    that don't vote (Solo/Party/ProposalWindow) simply don't implement it, so this adds NO required
    method to the `TurnArbiter` port. A decided vote is still enacted as ONE ordinary beat."""

    async def cast_vote(
        self, campaign_id: str, participant_id: str, choice: str
    ) -> VoteOutcome: ...

    async def resolve_pending(self, campaign_id: str) -> VoteOutcome | None:
        """Re-check a round WITHOUT a new vote — for when a DEPARTURE (a non-voter left), not a
        vote, is what completes the roster. Returns the decided outcome if it now resolves, else
        None. The server calls this after `note_left` so a stalled round still announces its
        result."""
        ...


class VoteArbiter(PartyArbiter):
    """Consensus/vote turns (D-38, G-11). Round-robin admission is inherited (a decided vote is
    enacted as the holder's ordinary beat — take_pending stays deferred), PLUS a session-only vote
    tally: `cast_vote` records one choice per participant (last-writer-wins) and DECIDES by strict
    plurality once every CURRENTLY-CONNECTED roster member has voted. A tie leaves it undecided
    (the party debates on the lane and re-votes) — deterministic, no clock/random. No vote burns a
    canonical beat. Session-only (D-31); a disconnect drops the in-flight tally."""

    def __init__(self) -> None:
        super().__init__()
        self._votes: dict[str, dict[str, str]] = {}  # campaign → participant → choice

    def _tally_and_decide(self, campaign_id: str) -> VoteOutcome:
        """The pure tally→decision step, shared by cast_vote and resolve_pending. Decides by STRICT
        plurality once every currently-connected roster member has voted; a tie stays undecided.
        Pops the round on a decision (reset for the next one)."""
        votes = self._votes.get(campaign_id, {})
        tally: Counter[str] = Counter(votes.values())
        roster = self._ring.get(campaign_id) or []
        decided: str | None = None
        if roster and votes and len(votes) >= len(roster):  # everyone connected has voted → resolve
            ranked = tally.most_common()  # insertion-order-stable for equal counts (deterministic)
            if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:  # a STRICT plurality (no tie)
                decided = ranked[0][0]
                self._votes.pop(campaign_id, None)  # reset for the next round
        return VoteOutcome(tally=dict(tally), decided=decided)

    async def cast_vote(self, campaign_id: str, participant_id: str, choice: str) -> VoteOutcome:
        self._votes.setdefault(campaign_id, {})[participant_id] = choice  # last-writer-wins
        return self._tally_and_decide(campaign_id)

    async def resolve_pending(self, campaign_id: str) -> VoteOutcome | None:
        # Called by the server after a departure: a non-voter leaving can make the remaining votes
        # a complete round that cast_vote never re-checks (it only fires on a vote). Only report a
        # NEWLY-decided round — a still-pending or tied one returns None (no spurious broadcast).
        if not self._votes.get(campaign_id):
            return None
        outcome = self._tally_and_decide(campaign_id)
        return outcome if outcome.decided is not None else None

    async def note_left(self, campaign_id: str, participant_id: str) -> None:
        await super().note_left(campaign_id, participant_id)
        # A departing member's own vote must not stall a round that is now 'everyone-voted' without
        # them — drop it. (If they held ≥1 other connection, PartyArbiter kept them in the ring, so
        # only fully-disconnected participants are pruned here — matched by the ring membership.)
        votes = self._votes.get(campaign_id)
        ring = self._ring.get(campaign_id) or []
        if votes and participant_id in votes and participant_id not in ring:
            del votes[participant_id]
            if not votes:
                self._votes.pop(campaign_id, None)
