"""Domain events — the source of truth (docs/03, 12).

Phase 0 carries only the two event types the walking skeleton needs: `WorldGenesis`
and `BeatResolved`. The envelope matches docs/12; more payload types arrive with the
event catalog in later phases.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from uro_core.domain.ids import new_id

Segment = Literal["morning", "afternoon", "evening", "night"]
CausedByKind = Literal["player_action", "agenda", "history", "ruleset", "system"]
Truth = Literal["true", "false", "unknown"]  # engine-level ground truth of a claim (docs/02)


class WorldTime(BaseModel):
    """In-fiction time: absolute day since epoch + segment (docs/12, D-22)."""

    day: int = 0
    segment: Segment = "morning"


HistoryPass = Literal["seeding", "adaptation", "backfill", "timeskip"]


class CausedBy(BaseModel):
    """Provenance of an event (docs/12). The `history` kind carries a `pass`
    discriminator (seeding|adaptation|backfill|timeskip) — serialized under the wire
    key `pass` (a Python keyword, so the field is `history_pass` with that alias)."""

    model_config = ConfigDict(populate_by_name=True)

    kind: CausedByKind
    participant_id: str | None = None
    beat_id: str | None = None
    history_pass: HistoryPass | None = Field(default=None, alias="pass")
    encounter_id: str | None = None  # ruleset kind: which encounter produced the effect


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


# --- Places: the slow-changing physical layer (docs/02, 12) ---
#
# Physical state is mutable via events — PlaceDestroyed/TerrainChanged are ordinary
# timeline events (D-4). The meteor test turns on exactly this: the crater is `true`
# on the aftermath branch and absent on a what-if fork taken before the strike.

PlaceKind = Literal["region", "settlement", "site"]
PlaceStatus = Literal["active", "destroyed"]


class PlaceCreatedPayload(BaseModel):
    v: int = 1
    place_id: str
    name: str
    kind: PlaceKind = "site"
    status: PlaceStatus = "active"
    description: str = ""


class PlaceStateChangedPayload(BaseModel):
    v: int = 1
    place_id: str
    changes: dict[str, Any] = Field(default_factory=dict)  # name/kind/status/description

    @model_validator(mode="after")
    def _validate_enum_changes(self) -> PlaceStateChangedPayload:
        # `changes` is deliberately open, but its enum-typed keys must clear the SAME bar
        # PlaceCreated does — otherwise the loose mutation path could project a status like
        # 'exploded' that state-checks (destroyed vs active — the meteor signal) misread.
        # Rejected at the sanctioned mint path, like BeliefChanged's confidence bound.
        for key, allowed in (("kind", get_args(PlaceKind)), ("status", get_args(PlaceStatus))):
            value = self.changes.get(key)
            if value is not None and value not in allowed:
                raise ValueError(f"invalid place {key} {value!r}; expected one of {allowed}")
        return self


class TerrainChangedPayload(BaseModel):
    v: int = 1
    place_id: str
    description: str
    effects: list[str] = Field(default_factory=list)


class PlaceDestroyedPayload(BaseModel):
    v: int = 1
    place_id: str
    cause: str = ""


def place_created(
    *,
    place_id: str,
    name: str,
    kind: PlaceKind = "site",
    status: PlaceStatus = "active",
    description: str = "",
    caused_by: CausedBy | None = None,
) -> DomainEvent:
    return DomainEvent(
        event_type="PlaceCreated",
        entity_refs=[place_id],
        caused_by=_default_cause(caused_by),
        payload=PlaceCreatedPayload(
            place_id=place_id, name=name, kind=kind, status=status, description=description
        ).model_dump(),
    )


def place_state_changed(
    *, place_id: str, changes: dict[str, Any], caused_by: CausedBy | None = None
) -> DomainEvent:
    return DomainEvent(
        event_type="PlaceStateChanged",
        entity_refs=[place_id],
        caused_by=_default_cause(caused_by),
        payload=PlaceStateChangedPayload(place_id=place_id, changes=changes).model_dump(),
    )


def terrain_changed(
    *,
    place_id: str,
    description: str,
    effects: list[str] | None = None,
    caused_by: CausedBy | None = None,
) -> DomainEvent:
    return DomainEvent(
        event_type="TerrainChanged",
        entity_refs=[place_id],
        caused_by=_default_cause(caused_by),
        payload=TerrainChangedPayload(
            place_id=place_id, description=description, effects=effects or []
        ).model_dump(),
    )


def place_destroyed(
    *, place_id: str, cause: str = "", caused_by: CausedBy | None = None
) -> DomainEvent:
    return DomainEvent(
        event_type="PlaceDestroyed",
        entity_refs=[place_id],
        caused_by=_default_cause(caused_by),
        payload=PlaceDestroyedPayload(place_id=place_id, cause=cause).model_dump(),
    )


def history_cause(pass_: HistoryPass) -> CausedBy:
    """Provenance for a History-service event (seeding/adaptation/timeskip, docs/12)."""
    # Via model_validate on the wire shape so the `pass` alias sets history_pass cleanly.
    return CausedBy.model_validate({"kind": "history", "pass": pass_})


# --- Campaign lifecycle & PC binding (docs/12; the fork's adopt-as-PC / retire-to-NPC) ---
#
# "Is this actor a PC?" is not a global flag — it is answered per-branch by the campaign's
# PCBound/PCReleased history (docs/02): the SAME actor_id can be a PC on one fork (the player
# who continues) and an ordinary NPC on a sibling fork (where someone else plays). Emitter S.


class CampaignStartedPayload(BaseModel):
    v: int = 1
    campaign_id: str
    branch_id: str
    party: list[str] = Field(default_factory=list)  # PC actor ids
    seed: int = 0


class CampaignEndedPayload(BaseModel):
    v: int = 1
    campaign_id: str
    outcome: str = ""
    marker_ref: str = ""


class PCBoundPayload(BaseModel):
    v: int = 1
    actor_id: str
    participant_id: str
    campaign_id: str


class PCReleasedPayload(BaseModel):
    v: int = 1
    actor_id: str
    participant_id: str
    campaign_id: str


def campaign_started(
    *,
    campaign_id: str,
    branch_id: str,
    party: list[str] | None = None,
    seed: int = 0,
    caused_by: CausedBy | None = None,
) -> DomainEvent:
    return DomainEvent(
        event_type="CampaignStarted",
        entity_refs=list(party or []),
        caused_by=_default_cause(caused_by),
        payload=CampaignStartedPayload(
            campaign_id=campaign_id, branch_id=branch_id, party=party or [], seed=seed
        ).model_dump(),
    )


def campaign_ended(
    *,
    campaign_id: str,
    outcome: str = "",
    marker_ref: str = "",
    caused_by: CausedBy | None = None,
) -> DomainEvent:
    return DomainEvent(
        event_type="CampaignEnded",
        caused_by=_default_cause(caused_by),
        payload=CampaignEndedPayload(
            campaign_id=campaign_id, outcome=outcome, marker_ref=marker_ref
        ).model_dump(),
    )


def pc_bound(
    *, actor_id: str, participant_id: str, campaign_id: str, caused_by: CausedBy | None = None
) -> DomainEvent:
    return DomainEvent(
        event_type="PCBound",
        entity_refs=[actor_id],
        caused_by=_default_cause(caused_by),
        payload=PCBoundPayload(
            actor_id=actor_id, participant_id=participant_id, campaign_id=campaign_id
        ).model_dump(),
    )


def pc_released(
    *, actor_id: str, participant_id: str, campaign_id: str, caused_by: CausedBy | None = None
) -> DomainEvent:
    return DomainEvent(
        event_type="PCReleased",
        entity_refs=[actor_id],
        caused_by=_default_cause(caused_by),
        payload=PCReleasedPayload(
            actor_id=actor_id, participant_id=participant_id, campaign_id=campaign_id
        ).model_dump(),
    )


# --- Time & History adaptation (docs/12; the fork time-skip) ---


class TimeAdvancedPayload(BaseModel):
    v: int = 1
    from_day: int
    to_day: int
    reason: str = ""


class AdaptationAppliedPayload(BaseModel):
    v: int = 1
    trigger_refs: list[str] = Field(default_factory=list)
    scope: str = ""
    summary: str = ""


def time_advanced(
    *, from_day: int, to_day: int, reason: str = "", caused_by: CausedBy | None = None
) -> DomainEvent:
    return DomainEvent(
        event_type="TimeAdvanced",
        world_time=WorldTime(day=to_day),
        caused_by=_default_cause(caused_by),
        payload=TimeAdvancedPayload(from_day=from_day, to_day=to_day, reason=reason).model_dump(),
    )


def adaptation_applied(
    *,
    trigger_refs: list[str] | None = None,
    scope: str = "",
    summary: str = "",
    to_day: int = 0,
    caused_by: CausedBy | None = None,
) -> DomainEvent:
    return DomainEvent(
        event_type="AdaptationApplied",
        world_time=WorldTime(day=to_day),
        caused_by=_default_cause(caused_by),
        payload=AdaptationAppliedPayload(
            trigger_refs=trigger_refs or [], scope=scope, summary=summary
        ).model_dump(),
    )


# --- Character sheets (docs/06, 12; ruleset-owned) ---
#
# The ruleset owns the sheet's SEMANTICS (docs/06 sheet_schema); the store records it opaquely,
# though the pipeline currently reads it via the shared port Sheet (port-fixed shape, OQ-13).
# Emitter R S. The PoC stores the FULL sheet per update (a whole-sheet replace, not the
# catalog's incremental "sheet_patch") — simplest sound rule until mechanics need partial patches.


class SheetUpdatedPayload(BaseModel):
    v: int = 1
    actor_id: str
    ruleset_id: str = ""
    sheet: dict[str, Any] = Field(default_factory=dict)


def sheet_updated(
    *,
    actor_id: str,
    sheet: dict[str, Any],
    ruleset_id: str = "",
    caused_by: CausedBy | None = None,
) -> DomainEvent:
    return DomainEvent(
        event_type="SheetUpdated",
        entity_refs=[actor_id],
        caused_by=_default_cause(caused_by),
        payload=SheetUpdatedPayload(
            actor_id=actor_id, ruleset_id=ruleset_id, sheet=sheet
        ).model_dump(),
    )


def ruleset_cause(encounter_id: str) -> CausedBy:
    """Provenance for a ruleset-emitted effect inside an encounter (docs/12, D-26)."""
    return CausedBy(kind="ruleset", encounter_id=encounter_id)


# --- Encounter mode & mechanical effects (docs/06, 12; emitter R for in-process resolution) ---
#
# The ruleset produces Effects (docs/06); the pipeline maps each to one of these R-emitted
# events so mechanics are ordinary timeline citizens. ActorDamaged reduces the sheet's hp
# projection; ItemTransferred moves ownership. Injuries and loot persist into later free-roam.


class ActorDamagedPayload(BaseModel):
    v: int = 1
    actor_id: str
    amount: int
    source: str = ""
    trace: str = ""


class ActorDiedPayload(BaseModel):
    v: int = 1
    actor_id: str
    cause: str = ""


class ItemCreatedPayload(BaseModel):
    v: int = 1
    item_id: str
    name: str
    owner_ref: str = ""
    kind: str = ""


class ItemTransferredPayload(BaseModel):
    v: int = 1
    item_id: str
    from_ref: str = ""
    to_ref: str = ""
    means: str = ""


class EncounterStartedPayload(BaseModel):
    v: int = 1
    encounter_id: str
    participants: list[str] = Field(default_factory=list)
    initiative: list[list[Any]] = Field(default_factory=list)  # [[actor_id, roll], ...]


class EncounterTurnTakenPayload(BaseModel):
    v: int = 1
    encounter_id: str
    actor_id: str
    action: str
    result: str = ""
    trace: str = ""


class EncounterEndedPayload(BaseModel):
    v: int = 1
    encounter_id: str
    outcome: dict[str, Any] = Field(default_factory=dict)


class ModeChangedPayload(BaseModel):
    v: int = 1
    from_mode: str
    to_mode: str
    cause: str = ""


def actor_damaged(
    *,
    actor_id: str,
    amount: int,
    source: str = "",
    trace: str = "",
    caused_by: CausedBy | None = None,
) -> DomainEvent:
    return DomainEvent(
        event_type="ActorDamaged",
        entity_refs=[actor_id],
        caused_by=_default_cause(caused_by),
        payload=ActorDamagedPayload(
            actor_id=actor_id, amount=amount, source=source, trace=trace
        ).model_dump(),
    )


def actor_died(*, actor_id: str, cause: str = "", caused_by: CausedBy | None = None) -> DomainEvent:
    return DomainEvent(
        event_type="ActorDied",
        entity_refs=[actor_id],
        caused_by=_default_cause(caused_by),
        payload=ActorDiedPayload(actor_id=actor_id, cause=cause).model_dump(),
    )


def item_created(
    *,
    item_id: str,
    name: str,
    owner_ref: str = "",
    kind: str = "",
    caused_by: CausedBy | None = None,
) -> DomainEvent:
    return DomainEvent(
        event_type="ItemCreated",
        entity_refs=[item_id, owner_ref] if owner_ref else [item_id],
        caused_by=_default_cause(caused_by),
        payload=ItemCreatedPayload(
            item_id=item_id, name=name, owner_ref=owner_ref, kind=kind
        ).model_dump(),
    )


def item_transferred(
    *,
    item_id: str,
    from_ref: str = "",
    to_ref: str = "",
    means: str = "",
    caused_by: CausedBy | None = None,
) -> DomainEvent:
    return DomainEvent(
        event_type="ItemTransferred",
        entity_refs=[item_id, from_ref, to_ref],
        caused_by=_default_cause(caused_by),
        payload=ItemTransferredPayload(
            item_id=item_id, from_ref=from_ref, to_ref=to_ref, means=means
        ).model_dump(),
    )


def encounter_started(
    *,
    encounter_id: str,
    participants: list[str],
    initiative: list[list[Any]] | None = None,
    caused_by: CausedBy | None = None,
) -> DomainEvent:
    return DomainEvent(
        event_type="EncounterStarted",
        entity_refs=list(participants),
        caused_by=_default_cause(caused_by),
        payload=EncounterStartedPayload(
            encounter_id=encounter_id, participants=participants, initiative=initiative or []
        ).model_dump(),
    )


def encounter_turn_taken(
    *,
    encounter_id: str,
    actor_id: str,
    action: str,
    result: str = "",
    trace: str = "",
    caused_by: CausedBy | None = None,
) -> DomainEvent:
    return DomainEvent(
        event_type="EncounterTurnTaken",
        entity_refs=[actor_id],
        caused_by=_default_cause(caused_by),
        payload=EncounterTurnTakenPayload(
            encounter_id=encounter_id, actor_id=actor_id, action=action, result=result, trace=trace
        ).model_dump(),
    )


def encounter_ended(
    *, encounter_id: str, outcome: dict[str, Any], caused_by: CausedBy | None = None
) -> DomainEvent:
    return DomainEvent(
        event_type="EncounterEnded",
        caused_by=_default_cause(caused_by),
        payload=EncounterEndedPayload(encounter_id=encounter_id, outcome=outcome).model_dump(),
    )


def mode_changed(
    *, from_mode: str, to_mode: str, cause: str = "", caused_by: CausedBy | None = None
) -> DomainEvent:
    return DomainEvent(
        event_type="ModeChanged",
        caused_by=_default_cause(caused_by),
        payload=ModeChangedPayload(from_mode=from_mode, to_mode=to_mode, cause=cause).model_dump(),
    )
