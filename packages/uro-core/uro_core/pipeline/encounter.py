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
seed), so a fight replays byte-identically (docs/10).

TURN BY TURN (D-112, for a campaign that asks for it): `open_fight` starts the same loop but stops
on a player character's turn, and `take_turn` plays the player's chosen action, then the ruleset's
own turns up to the next player's. Nothing of the ruleset's opaque state is stored: each turn
REPLAYS the fight from its EncounterStarted (the seed, the combatants as they entered) and the
choices recorded on its turns, which reproduces the same state and dice stream because every
ruleset method is a pure function of its inputs and the Rng. A fork taken mid-fight carries it."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from uro_core.domain.events import (
    DomainEvent,
    actor_died,
    encounter_ended,
    encounter_started,
    encounter_turn_taken,
    ruleset_cause,
    sheet_updated,
)
from uro_core.rulesets.base import (
    Action,
    ActionSpec,
    Combatant,
    Effect,
    EncounterCtx,
    EncounterOutcome,
    EncounterState,
    Ruleset,
)
from uro_core.rulesets.rng import Rng

_MAX_TURNS = 200


def _turn_event(
    encounter_id: str,
    actor_id: str,
    action: Action,
    effects: list[Effect],
    cause: Any,
    *,
    chosen: bool = False,
) -> DomainEvent:
    # The ruleset names its own effect kinds (uro_basic: hit/miss/down; uro_pbta:
    # full/partial/miss); the runner just surfaces the last one as the turn's result label.
    result = effects[-1].kind if effects else "act"
    # EVERY effect's trace: a turn may resolve several strikes at once (uro_pool's simultaneous
    # exchange closes the whole round on its last commit), and the narrator must see them all.
    trace = "; ".join(e.trace for e in effects if e.trace)
    return encounter_turn_taken(
        encounter_id=encounter_id,
        actor_id=actor_id,
        action=action.kind,
        result=result,
        trace=trace,
        chosen=action.model_dump() if chosen else None,
        caused_by=cause,
    )


def _waits_on(ruleset: Ruleset, state: EncounterState, players: frozenset[str]) -> str | None:
    """The player character whose turn it is and who has a choice to make, else None."""
    actor = ruleset.current_actor(state)
    if actor in players and ruleset.legal_actions(state, actor):
        return actor
    return None


def _advance(
    ruleset: Ruleset,
    state: EncounterState,
    rng: Rng,
    *,
    encounter_id: str,
    cause: Any,
    players: frozenset[str] = frozenset(),
    budget: int = _MAX_TURNS,
) -> tuple[EncounterState, list[DomainEvent]]:
    """The ruleset's own turns, until the fight is decided or a player has a choice to make."""
    events: list[DomainEvent] = []
    for _ in range(budget):
        actor_id = ruleset.current_actor(state)
        if actor_id is None or _waits_on(ruleset, state, players) is not None:
            break
        action = ruleset.npc_action(state, actor_id, rng)
        state, effects = ruleset.resolve_action(state, action, rng)
        events.append(_turn_event(encounter_id, actor_id, action, effects, cause))
    return state, events


def _conclude(
    ruleset: Ruleset,
    state: EncounterState,
    combatants: list[Combatant],
    *,
    encounter_id: str,
    cause: Any,
    lethal: bool,
) -> tuple[list[DomainEvent], EncounterOutcome]:
    outcome = ruleset.is_over(state) or EncounterOutcome()
    # Persist ruleset-shaped harm as OPAQUE final sheets — the timeline never assumes hp (D-30) —
    # after the ruleset's own aftermath (advancement for the player characters, D-63).
    pcs = frozenset(c.actor_id for c in combatants if c.is_pc)
    final = ruleset.conclude_encounter(state, outcome, pcs)
    events: list[DomainEvent] = [
        sheet_updated(
            actor_id=actor_id, sheet=final[actor_id], ruleset_id=ruleset.id, caused_by=cause
        )
        for actor_id in sorted(final)
    ]
    if lethal:
        for actor_id in outcome.out_of_fight:
            events.append(actor_died(actor_id=actor_id, caused_by=cause))
    events.append(
        encounter_ended(encounter_id=encounter_id, outcome=outcome.model_dump(), caused_by=cause)
    )
    return events, outcome


def run_encounter(
    ruleset: Ruleset,
    combatants: list[Combatant],
    rng: Rng,
    *,
    encounter_id: str,
    lethal: bool = False,
    max_turns: int = _MAX_TURNS,
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
    state, turns = _advance(
        ruleset, state, rng, encounter_id=encounter_id, cause=cause, budget=max_turns
    )
    ending, outcome = _conclude(
        ruleset, state, combatants, encounter_id=encounter_id, cause=cause, lethal=lethal
    )
    return [*events, *turns, *ending], outcome


class FightStep(BaseModel):
    """What one beat of a turn-by-turn fight did (D-112): its events, and either the decided
    outcome or the player character it now waits on, with that character's legal actions."""

    model_config = {"arbitrary_types_allowed": True}

    encounter_id: str
    events: list[DomainEvent] = Field(default_factory=list)
    outcome: EncounterOutcome | None = None
    waiting_on: str | None = None
    legal_actions: list[ActionSpec] = Field(default_factory=list)


class ChoiceRefused(ValueError):
    """A player's encounter action that can't be played now (D-112): not their turn, or not one
    of the legal actions, which are carried so a client can offer them."""

    def __init__(self, reason: str, legal_actions: list[ActionSpec] | None = None) -> None:
        super().__init__(reason)
        self.legal_actions = legal_actions or []


class TurnChoice(BaseModel):
    """A player's action on their character's turn: one of the ruleset's `legal_actions` kinds,
    a target among that action's targets (or none), and any extras the ruleset reads."""

    kind: str
    target_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


def _step(
    ruleset: Ruleset,
    state: EncounterState,
    rng: Rng,
    combatants: list[Combatant],
    events: list[DomainEvent],
    *,
    encounter_id: str,
    cause: Any,
) -> FightStep:
    players = frozenset(c.actor_id for c in combatants if c.is_pc)
    state, turns = _advance(
        ruleset, state, rng, encounter_id=encounter_id, cause=cause, players=players
    )
    events = [*events, *turns]
    waiting = _waits_on(ruleset, state, players)
    if waiting is not None and ruleset.is_over(state) is None:
        return FightStep(
            encounter_id=encounter_id,
            events=events,
            waiting_on=waiting,
            legal_actions=ruleset.legal_actions(state, waiting),
        )
    ending, outcome = _conclude(
        ruleset, state, combatants, encounter_id=encounter_id, cause=cause, lethal=False
    )
    return FightStep(encounter_id=encounter_id, events=[*events, *ending], outcome=outcome)


def open_fight(
    ruleset: Ruleset, combatants: list[Combatant], rng: Rng, *, encounter_id: str
) -> FightStep:
    """Start a fight to be played turn by turn: the ruleset's turns run until a player character
    has a choice (or the fight is decided before one does). `rng` must be fresh: its seed is
    recorded so later turns replay the same dice."""
    cause = ruleset_cause(encounter_id)
    state = ruleset.start_encounter(
        EncounterCtx(encounter_id=encounter_id, combatants=combatants), rng
    )
    started = encounter_started(
        encounter_id=encounter_id,
        participants=[c.actor_id for c in combatants],
        interactive=True,
        seed=rng.seed,
        combatants=[c.model_dump() for c in combatants],
        caused_by=cause,
    )
    return _step(ruleset, state, rng, combatants, [started], encounter_id=encounter_id, cause=cause)


def replay_fight(
    ruleset: Ruleset, started: dict[str, Any], turns: list[dict[str, Any]]
) -> tuple[EncounterState, Rng, list[Combatant]]:
    """Rebuild an open fight's state from its EncounterStarted payload and its turns so far: the
    same start and the same actions (a player's recorded choice, or the ruleset's own, decided
    again) on the same dice stream."""
    rng = Rng(int(started["seed"]))
    combatants = [Combatant.model_validate(c) for c in started["combatants"]]
    state = ruleset.start_encounter(
        EncounterCtx(encounter_id=started["encounter_id"], combatants=combatants), rng
    )
    for t in turns:
        actor_id = ruleset.current_actor(state)
        if actor_id != t["actor_id"]:
            raise RuntimeError(
                f"encounter {started['encounter_id']} doesn't replay: turn for {t['actor_id']!r}, "
                f"the ruleset says {actor_id!r} (was the ruleset changed mid-fight?)"
            )
        chosen = t.get("chosen")
        action = (
            Action.model_validate(chosen)
            if chosen
            else ruleset.npc_action(state, t["actor_id"], rng)
        )
        state, _ = ruleset.resolve_action(state, action, rng)
    return state, rng, combatants


def take_turn(
    ruleset: Ruleset,
    started: dict[str, Any],
    turns: list[dict[str, Any]],
    actor_id: str,
    choice: TurnChoice,
) -> FightStep:
    """Play `actor_id`'s chosen action on its turn, then the ruleset's turns up to the next
    player's choice or the end. Refuses (`ChoiceRefused`) an action out of turn, or one that
    isn't among the legal actions or their targets."""
    encounter_id = started["encounter_id"]
    cause = ruleset_cause(encounter_id)
    state, rng, combatants = replay_fight(ruleset, started, turns)
    current = ruleset.current_actor(state)
    legal = ruleset.legal_actions(state, current) if current else []
    if current != actor_id:
        raise ChoiceRefused(f"it isn't {actor_id}'s turn (it's {current}'s)", [])
    spec = next((a for a in legal if a.kind == choice.kind), None)
    if spec is None:
        kinds = ", ".join(a.kind for a in legal) or "none"
        raise ChoiceRefused(f"{choice.kind!r} isn't a legal action now (legal: {kinds})", legal)
    target = choice.target_id
    if target is None and len(spec.targets) == 1:
        target = spec.targets[0]
    if spec.targets and target not in spec.targets:
        raise ChoiceRefused(
            f"{choice.kind!r} needs a target among {', '.join(spec.targets)}", legal
        )
    action = Action(kind=choice.kind, actor_id=actor_id, target_id=target, data=choice.data)
    state, effects = ruleset.resolve_action(state, action, rng)
    turn = _turn_event(encounter_id, actor_id, action, effects, cause, chosen=True)
    return _step(ruleset, state, rng, combatants, [turn], encounter_id=encounter_id, cause=cause)
