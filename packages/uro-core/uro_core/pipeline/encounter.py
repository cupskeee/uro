"""Encounter runner (docs/05, 06, 12). Deterministic — no LLM. GAME-AGNOSTIC (D-30).

PoC AUTO-RESOLVE (D-29, which narrows D-26 for the PoC): the pipeline drives the whole loop, and
EVERY turn's action — PC turns included — comes from the ruleset's deterministic `npc_action`;
there is no interactive per-turn player action yet. The runner NEVER introspects the ruleset's
encounter state: it walks turns via `current_actor` and stops on `is_over`, so a d20 initiative
grind (uro_basic) and a PbtA move-exchange (uro_pbta) run through the identical loop.

Harm is game-shape-agnostic: instead of mapping typed effects to an hp-scalar event, the runner
persists each combatant's OPAQUE final sheet as `SheetUpdated` (the ruleset computed whatever
its harm model is — hp, a harm clock, conditions). `ActorDied` is emitted only for a lethal
encounter, as a ruleset-agnostic lifecycle trace. Everything is a pure function of (combatants,
seed), so a fight replays byte-identically (docs/10)."""

from __future__ import annotations

from uro_core.domain.events import (
    DomainEvent,
    actor_died,
    encounter_ended,
    encounter_started,
    encounter_turn_taken,
    ruleset_cause,
    sheet_updated,
)
from uro_core.rulesets.base import Combatant, EncounterCtx, EncounterOutcome, Ruleset
from uro_core.rulesets.rng import Rng


def run_encounter(
    ruleset: Ruleset,
    combatants: list[Combatant],
    rng: Rng,
    *,
    encounter_id: str,
    lethal: bool = False,
    max_turns: int = 200,
) -> tuple[list[DomainEvent], EncounterOutcome]:
    """Auto-resolve an encounter to completion. Returns (events, outcome). `lethal=False`
    (the default) leaves an out-of-fight combatant merely incapacitated — no `ActorDied` — so a
    lost brawl leaves an injury, not a corpse; the caller adds interpretive consequences."""
    cause = ruleset_cause(encounter_id)
    state = ruleset.start_encounter(
        EncounterCtx(encounter_id=encounter_id, combatants=combatants), rng
    )
    events: list[DomainEvent] = [
        encounter_started(
            encounter_id=encounter_id,
            participants=[c.actor_id for c in combatants],
            caused_by=cause,
        )
    ]

    for _ in range(max_turns):
        actor_id = ruleset.current_actor(state)
        if actor_id is None:
            break
        action = ruleset.npc_action(state, actor_id, rng)
        state, effects = ruleset.resolve_action(state, action, rng)
        # The ruleset names its own effect kinds (uro_basic: hit/miss/down; uro_pbta:
        # full/partial/miss); the runner just surfaces the last one as the turn's result label.
        result = effects[-1].kind if effects else "act"
        trace = next((e.trace for e in effects if e.trace), "")
        events.append(
            encounter_turn_taken(
                encounter_id=encounter_id,
                actor_id=actor_id,
                action=action.kind,
                result=result,
                trace=trace,
                caused_by=cause,
            )
        )

    outcome = ruleset.is_over(state) or EncounterOutcome()
    # Persist ruleset-shaped harm as OPAQUE final sheets — the timeline never assumes hp (D-30).
    final = ruleset.sheets(state)
    for actor_id in sorted(final):
        events.append(
            sheet_updated(
                actor_id=actor_id, sheet=final[actor_id], ruleset_id=ruleset.id, caused_by=cause
            )
        )
    if lethal:
        for actor_id in outcome.out_of_fight:
            events.append(actor_died(actor_id=actor_id, caused_by=cause))
    events.append(
        encounter_ended(encounter_id=encounter_id, outcome=outcome.model_dump(), caused_by=cause)
    )
    return events, outcome
