"""Chronicler mode (docs/05, D-25): distill an EXTERNAL game's outcome into Uro's world memory.

In Chronicler mode Uro does NOT run the fight — an external game does ("can I hit it, for how
much?") and reports an `OutcomeBundle`. Uro answers "who knows what, and how does it change the
story?": rule-based distillation turns the bundle into committed events (emitter E, external
trust tier), then belief-propagates each notable feat to the surviving witnesses (docs/02) — so
feats become witness beliefs become rumors. No LLM.

This is the tiny Phase-5 proof of D-25's Chronicler door; the full ingestion contract (OQ-12) is
deferred. The bundle is trusted-but-scoped: it may report mechanical facts (feats, casualties,
loot) but the interpretive retelling still flows through the ordinary narrator + belief model.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from uro_core.domain.events import (
    DomainEvent,
    actor_died,
    claim_recorded,
    external_cause,
    item_transferred,
)
from uro_core.domain.ids import new_id
from uro_core.engines.actor import propagate_belief
from uro_core.ports.projections import ProjectionQueries


class Feat(BaseModel):
    actor: str  # the actor who performed the deed
    description: str  # "a lone wizard split the warband's champion in two"


class LootTransfer(BaseModel):
    item_id: str
    from_ref: str = ""
    to_ref: str = ""


class OutcomeBundle(BaseModel):
    """Outcome bundle v0 (docs/05, D-25) — what an external game reports after resolving an
    encounter in its own domain."""

    v: int = 1
    encounter_id: str
    participants: list[str] = Field(default_factory=list)
    witnesses: list[str] = Field(default_factory=list)  # SURVIVORS who saw it (drive rumors)
    casualties: list[str] = Field(default_factory=list)
    feats: list[Feat] = Field(default_factory=list)
    loot: list[LootTransfer] = Field(default_factory=list)
    duration_rounds: int = 0


async def distill_outcome(
    store: ProjectionQueries, branch_id: str, bundle: OutcomeBundle
) -> list[DomainEvent]:
    """Distill an outcome bundle into committable events (emitter E) + the witness rumor cascade.
    The caller commits them. Feats become `truth=true` claims and propagate to the witnesses;
    casualties/loot become the ordinary mechanical events."""
    cause = external_cause(bundle.encounter_id)
    events: list[DomainEvent] = []
    for feat in bundle.feats:
        claim_id = f"c:{new_id()}"
        events.append(
            claim_recorded(
                claim_id=claim_id,
                statement=feat.description,
                subject_refs=[feat.actor],
                truth="true",
                origin="chronicle",
                caused_by=cause,
            )
        )
        # feat → the surviving witnesses' beliefs → rumors down the contact graph (docs/02)
        events.extend(
            await propagate_belief(store, branch_id, claim_id=claim_id, witnesses=bundle.witnesses)
        )
    for casualty in bundle.casualties:
        events.append(actor_died(actor_id=casualty, cause="fell in the battle", caused_by=cause))
    for loot in bundle.loot:
        events.append(
            item_transferred(
                item_id=loot.item_id,
                from_ref=loot.from_ref,
                to_ref=loot.to_ref,
                caused_by=cause,
            )
        )
    return events
