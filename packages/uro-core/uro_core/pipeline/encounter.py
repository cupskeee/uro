"""Encounter runner (docs/05, 06, 12). Deterministic — no LLM.

PoC AUTO-RESOLVE (D-29, which narrows D-26 for the PoC): the pipeline drives the whole
initiative loop, and EVERY turn's action — PC turns included — comes from the ruleset's
deterministic `npc_action` (D-26's chooser); there is no interactive per-turn player action
yet. Each turn is recorded as `EncounterTurnTaken`, and the ruleset's Effects are mapped to
R-emitted domain events (`ActorDamaged` / `ActorDied`) so mechanics are ordinary timeline
citizens — HP damage lands in the sheet projection and persists into later free-roam. The
interactive design (PC turns from the player, each turn a beat, `encounter_action`) is deferred
(D-29). Everything here is a pure function of (combatants, seed), so a fight replays
byte-identically (docs/10)."""

from __future__ import annotations

from uro_core.domain.events import (
    DomainEvent,
    actor_damaged,
    actor_died,
    encounter_ended,
    encounter_started,
    encounter_turn_taken,
    ruleset_cause,
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
    (the default) leaves a downed combatant merely incapacitated (hp 0) — no `ActorDied` — so
    a lost brawl leaves an injury, not a corpse; the caller adds interpretive consequences."""
    cause = ruleset_cause(encounter_id)
    state = ruleset.start_encounter(
        EncounterCtx(encounter_id=encounter_id, combatants=combatants), rng
    )
    events: list[DomainEvent] = [
        encounter_started(
            encounter_id=encounter_id,
            participants=list(state.combatants),
            initiative=[[a, state.combatants[a].initiative] for a in state.order],
            caused_by=cause,
        )
    ]

    for _ in range(max_turns):
        if ruleset.is_over(state) is not None or state.over:
            break
        actor_id = state.current_actor()
        action = ruleset.npc_action(state, actor_id, rng)
        state, effects = ruleset.resolve_action(state, action, rng)

        dmg = next((e for e in effects if e.kind == "damage"), None)
        downed = any(e.kind == "death" for e in effects)
        result = (
            "down" if downed else "hit" if (dmg and dmg.amount > 0) else "miss" if dmg else "act"
        )
        events.append(
            encounter_turn_taken(
                encounter_id=encounter_id,
                actor_id=actor_id,
                action=action.kind,
                result=result,
                trace=dmg.trace if dmg else "",
                caused_by=cause,
            )
        )
        for e in effects:
            if e.kind == "damage" and e.amount > 0:
                events.append(
                    actor_damaged(
                        actor_id=e.actor_id,
                        amount=e.amount,
                        source=e.source,
                        trace=e.trace,
                        caused_by=cause,
                    )
                )
            elif e.kind == "death" and lethal:
                events.append(actor_died(actor_id=e.actor_id, cause=e.source, caused_by=cause))

    outcome = ruleset.is_over(state) or EncounterOutcome(
        winner_team=None, survivors=[], casualties=[]
    )
    events.append(
        encounter_ended(encounter_id=encounter_id, outcome=outcome.model_dump(), caused_by=cause)
    )
    return events, outcome
