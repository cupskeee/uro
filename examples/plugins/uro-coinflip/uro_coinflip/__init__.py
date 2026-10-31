"""CoinFlip — an example Uro ruleset plugin (D-65). Every check and every blow is a coin flip.

Small but complete: it implements the whole ruleset port, so a world can play it end to end.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from uro_core.rulesets.base import (
    RULESET_API_VERSION,
    Action,
    ActionSpec,
    Affordance,
    Award,
    CharSpec,
    CheckRequest,
    CheckResult,
    Effect,
    EncounterCtx,
    EncounterOutcome,
    EncounterState,
)
from uro_core.rulesets.rng import Rng

Sheet = dict[str, Any]


class CoinConfig(BaseModel):
    """CoinFlip's settings — a world's `[ruleset.config]`. Unknown keys are refused."""

    model_config = ConfigDict(extra="forbid")

    coins: int = Field(1, ge=1, le=5, description="coins flipped; one heads succeeds")
    wins_per_fight: int = Field(1, ge=0, description="wins a PC records for winning a fight")


class _Fight(EncounterState):
    standing: dict[str, bool]
    teams: dict[str, str]
    order: list[str]
    turn: int = 0
    sheets: dict[str, Sheet]


class CoinFlip:
    id = "coinflip"
    version = "1"
    api_version = RULESET_API_VERSION

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = CoinConfig.model_validate(config or {})

    # --- character model ---

    def sheet_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"wins": {"type": "integer"}}}

    def config_schema(self) -> dict[str, Any]:
        return CoinConfig.model_json_schema()

    def new_character(self, spec: CharSpec, rng: Rng) -> Sheet:
        return {"wins": int(spec.data.get("wins", 0))}

    def progression(self, sheet: Sheet, award: Award) -> Sheet:
        return {**sheet, "wins": int(sheet.get("wins", 0)) + int(award.data.get("wins", 0))}

    # --- free-roam checks ---

    def affordances(self) -> list[Affordance]:
        return [
            Affordance(id="gamble", trigger_categories=["search"], description="trust to luck"),
            Affordance(
                id="swing",
                trigger_categories=["violence"],
                starts_encounter=True,
                description="start a fight",
            ),
        ]

    def _heads(self, rng: Rng) -> bool:
        return any(rng.die(2) == 1 for _ in range(self.config.coins))

    def resolve_check(self, req: CheckRequest, rng: Rng) -> CheckResult:
        outcome = "heads" if self._heads(rng) else "tails"
        return CheckResult(outcome=outcome, trace=f"{self.config.coins} coin(s) → {outcome}")

    # --- encounters: alternate flips; heads knocks the foe out ---

    def start_encounter(self, ctx: EncounterCtx, rng: Rng) -> EncounterState:
        return _Fight(
            encounter_id=ctx.encounter_id,
            standing={c.actor_id: True for c in ctx.combatants},
            teams={c.actor_id: c.team for c in ctx.combatants},
            order=[c.actor_id for c in ctx.combatants],
            sheets={c.actor_id: dict(c.sheet) for c in ctx.combatants},
        )

    def current_actor(self, state: EncounterState) -> str | None:
        st = _fight(state)
        if self.is_over(st) is not None:
            return None
        return st.order[st.turn % len(st.order)]

    def legal_actions(self, state: EncounterState, actor_id: str) -> list[ActionSpec]:
        return [ActionSpec(kind="flip", targets=_foes(_fight(state), actor_id))]

    def npc_action(self, state: EncounterState, actor_id: str, rng: Rng) -> Action:
        foes = _foes(_fight(state), actor_id)
        return Action(kind="flip", actor_id=actor_id, target_id=foes[0] if foes else None)

    def resolve_action(
        self, state: EncounterState, action: Action, rng: Rng
    ) -> tuple[EncounterState, list[Effect]]:
        st = _fight(state)
        standing = dict(st.standing)
        effects: list[Effect] = []
        if action.target_id is not None and self._heads(rng):
            standing[action.target_id] = False
            effects.append(
                Effect(kind="heads", actor_id=action.target_id, trace=f"{action.actor_id}: heads")
            )
        else:
            effects.append(Effect(kind="tails", actor_id=action.actor_id, trace="tails"))
        turn = st.turn + 1
        while not standing[st.order[turn % len(st.order)]] and any(standing.values()):
            turn += 1
        return st.model_copy(update={"standing": standing, "turn": turn}), effects

    def is_over(self, state: EncounterState) -> EncounterOutcome | None:
        st = _fight(state)
        live = {st.teams[a] for a, up in st.standing.items() if up}
        if len(live) > 1:
            return None
        return EncounterOutcome(
            winner_team=next(iter(live)) if live else None,
            survivors=sorted(a for a, up in st.standing.items() if up),
            out_of_fight=sorted(a for a, up in st.standing.items() if not up),
        )

    def sheets(self, state: EncounterState) -> dict[str, Sheet]:
        return {a: dict(s) for a, s in _fight(state).sheets.items()}

    def conclude_encounter(
        self, state: EncounterState, outcome: EncounterOutcome, pcs: frozenset[str]
    ) -> dict[str, Sheet]:
        final = self.sheets(state)
        award = Award(data={"wins": self.config.wins_per_fight})
        for actor_id in sorted(pcs & set(outcome.survivors)):
            final[actor_id] = self.progression(final[actor_id], award)
        return final

    def downtime(self, sheet: Sheet, days: int) -> Sheet | None:
        return None


def _fight(state: EncounterState) -> _Fight:
    return state if isinstance(state, _Fight) else _Fight.model_validate(state.model_dump())


def _foes(st: _Fight, actor_id: str) -> list[str]:
    me = st.teams[actor_id]
    return sorted(a for a, t in st.teams.items() if t != me and st.standing[a])
