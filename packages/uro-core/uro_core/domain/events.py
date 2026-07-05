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
