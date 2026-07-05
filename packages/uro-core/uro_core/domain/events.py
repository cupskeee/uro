"""Domain events — the source of truth (docs/03, 12).

Phase 0 carries only the two event types the walking skeleton needs: `WorldGenesis`
and `BeatResolved`. The envelope matches docs/12; more payload types arrive with the
event catalog in later phases.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from uro_core.domain.ids import new_id

Segment = Literal["morning", "afternoon", "evening", "night"]
CausedByKind = Literal["player_action", "agenda", "history", "ruleset", "system"]
Truth = Literal["true", "false", "unknown"]  # engine-level ground truth of a claim (docs/02)


class WorldTime(BaseModel):
    """In-fiction time: absolute day since epoch + segment (docs/12, D-22)."""

    day: int = 0
    segment: Segment = "morning"


class CausedBy(BaseModel):
    """Provenance of an event (docs/12). Phase 0 uses `system` and `player_action`."""

    kind: CausedByKind
    participant_id: str | None = None
    beat_id: str | None = None


class DomainEvent(BaseModel):
    """One typed, append-only fact. `payload` is validated per type by helpers below."""

    event_id: str = Field(default_factory=new_id)
    event_type: str
    entity_refs: list[str] = Field(default_factory=list)
    world_time: WorldTime = Field(default_factory=WorldTime)
    caused_by: CausedBy
    payload: dict[str, Any]


# --- Payload models (validated shapes; stored as the envelope's `payload` dict) ---


class WorldGenesisPayload(BaseModel):
    v: int = 1
    world_name: str


class BeatResolvedPayload(BaseModel):
    v: int = 1
    beat_id: str
    participant_id: str
    intent_text: str
    narration: str
    # short recap for the chronicle; populated once a summarizer exists (docs/12)
    synopsis: str = ""


# --- Constructors (the only sanctioned way to mint these events) ---


def world_genesis(world_name: str) -> DomainEvent:
    return DomainEvent(
        event_type="WorldGenesis",
        caused_by=CausedBy(kind="system"),
        payload=WorldGenesisPayload(world_name=world_name).model_dump(),
    )


def beat_resolved(
    *,
    beat_id: str,
    participant_id: str,
    intent_text: str,
    narration: str,
    synopsis: str = "",
) -> DomainEvent:
    return DomainEvent(
        event_type="BeatResolved",
        caused_by=CausedBy(kind="player_action", participant_id=participant_id, beat_id=beat_id),
        payload=BeatResolvedPayload(
            beat_id=beat_id,
            participant_id=participant_id,
            intent_text=intent_text,
            narration=narration,
            synopsis=synopsis,
        ).model_dump(),
    )


# --- Epistemic layer: actors, claims, beliefs (docs/02, 12) ---
#
# Phase 1 subset of the catalog. These are the events the extractor proposes and
# that drive the projections structured recall reads back. `_default_cause` keeps
# constructors ergonomic while letting the pipeline pass a real player_action cause.


def _default_cause(caused_by: CausedBy | None) -> CausedBy:
    return caused_by or CausedBy(kind="system")


class ActorCreatedPayload(BaseModel):
    v: int = 1
    actor_id: str
    name: str
    tier: int = Field(default=1, ge=0, le=3)  # T0 extra … T3 agent (docs/02)
    role: str = ""
    aliases: list[str] = Field(default_factory=list)


class ActorPromotedPayload(BaseModel):
    v: int = 1
    actor_id: str
    from_tier: int = Field(ge=0, le=3)
    to_tier: int = Field(ge=0, le=3)
    reason: str


class ClaimRecordedPayload(BaseModel):
    v: int = 1
    claim_id: str
    statement: str
    subject_refs: list[str] = Field(default_factory=list)
    truth: Truth = "unknown"
    origin: str = ""  # what produced it (event/actor ref, or "narration")


class ClaimTruthChangedPayload(BaseModel):
    v: int = 1
    claim_id: str
    truth: Truth
    cause: str = ""


class BeliefChangedPayload(BaseModel):
    v: int = 1
    actor_id: str
    claim_id: str
    # how strongly the actor holds the claim. Bounded at the sanctioned mint path so
    # extractor-produced garbage (>1, <0, NaN, inf) is rejected before reaching state.
    confidence: float = Field(ge=0.0, le=1.0)
    learned_from: str | None = None


def actor_created(
    *,
    actor_id: str,
    name: str,
    tier: int = 1,
    role: str = "",
    aliases: list[str] | None = None,
    caused_by: CausedBy | None = None,
) -> DomainEvent:
    return DomainEvent(
        event_type="ActorCreated",
        entity_refs=[actor_id],
        caused_by=_default_cause(caused_by),
        payload=ActorCreatedPayload(
            actor_id=actor_id, name=name, tier=tier, role=role, aliases=aliases or []
        ).model_dump(),
    )


def actor_promoted(
    *, actor_id: str, from_tier: int, to_tier: int, reason: str, caused_by: CausedBy | None = None
) -> DomainEvent:
    return DomainEvent(
        event_type="ActorPromoted",
        entity_refs=[actor_id],
        caused_by=_default_cause(caused_by),
        payload=ActorPromotedPayload(
            actor_id=actor_id, from_tier=from_tier, to_tier=to_tier, reason=reason
        ).model_dump(),
    )


def claim_recorded(
    *,
    claim_id: str,
    statement: str,
    subject_refs: list[str] | None = None,
    truth: Truth = "unknown",
    origin: str = "",
    caused_by: CausedBy | None = None,
) -> DomainEvent:
    refs = subject_refs or []
    return DomainEvent(
        event_type="ClaimRecorded",
        entity_refs=[claim_id, *refs],
        caused_by=_default_cause(caused_by),
        payload=ClaimRecordedPayload(
            claim_id=claim_id, statement=statement, subject_refs=refs, truth=truth, origin=origin
        ).model_dump(),
    )


def claim_truth_changed(
    *, claim_id: str, truth: Truth, cause: str = "", caused_by: CausedBy | None = None
) -> DomainEvent:
    return DomainEvent(
        event_type="ClaimTruthChanged",
        entity_refs=[claim_id],
        caused_by=_default_cause(caused_by),
        payload=ClaimTruthChangedPayload(claim_id=claim_id, truth=truth, cause=cause).model_dump(),
    )


def belief_changed(
    *,
    actor_id: str,
    claim_id: str,
    confidence: float,
    learned_from: str | None = None,
    caused_by: CausedBy | None = None,
) -> DomainEvent:
    return DomainEvent(
        event_type="BeliefChanged",
        entity_refs=[actor_id, claim_id],
        caused_by=_default_cause(caused_by),
        payload=BeliefChangedPayload(
            actor_id=actor_id, claim_id=claim_id, confidence=confidence, learned_from=learned_from
        ).model_dump(),
    )
