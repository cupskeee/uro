"""Uro PbtA — the alien second ruleset (OQ-13, D-30). Pure, deterministic, no LLM (docs/06).

Structurally opposite to Uro Basic on every axis, to probe the port for leaked d20 assumptions:

    d20 (uro_basic)                    PbtA (uro_pbta)
    ------------------                 ------------------------------
    6 ability scores, mod=(s-10)//2    4 stats, the stat IS the modifier
    d20 + mod vs a DC                  2d6 + stat vs fixed 7/10 tiers
    binary success/failure             miss / PARTIAL (success-at-a-cost) / full
    hp + ac, damage dice               a 0-4 harm clock + narrative conditions
    initiative, rounds, attrition      a move-exchange, no initiative, no rounds
    attack/defend/flee                 moves (seize_by_force, persuade, …)
    advance by levelling (winning)     mark XP on a MISS (advance by failing)

If the generic port hosts this with no changes of its own, it earned "game-agnostic".
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from uro_core.rulesets.base import (
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
from uro_core.rulesets.base import Sheet as SheetDict
from uro_core.rulesets.rng import Rng

STATS: tuple[str, ...] = ("forceful", "clever", "quick", "steady")
_DEFAULT_STATS: dict[str, int] = {"forceful": 1, "clever": 1, "quick": 0, "steady": 0}
_HARM_MAX = 4  # the harm clock: at 4 you are out of the fight (not necessarily dead)
_XP_PER_ADVANCE = 5
_STAT_CAP = 3
_EXPOSED = "Exposed"  # the standing cost a 7-9 in a conflict leaves on the mover


class Sheet(BaseModel):
    """Uro PbtA's OWN sheet — NO hp, NO ac. Harm is a 0-4 clock; setbacks are conditions; XP
    accrues from misses. The generic port holds only an opaque dict; this is what uro_pbta
    validates it into."""

    stats: dict[str, int]
    harm: int = 0
    conditions: list[str] = []
    xp: int = 0

    @property
    def in_fight(self) -> bool:
        return self.harm < _HARM_MAX


class _Fighter(BaseModel):
    actor_id: str
    team: str
    sheet: Sheet


class _Encounter(EncounterState):
    """Uro PbtA's private conflict state — an alternating move-exchange. NO initiative rolls and
    NO rounds (the two structural d20 features the port must not require): `order` is just the
    combatants as given, `turn_index` walks the still-standing ones."""

    fighters: dict[str, _Fighter]
    order: list[str]
    turn_index: int = 0


class UroPbtA:
    id = "uro-pbta"
    version = "0"

    def sheet_schema(self) -> dict[str, Any]:
        return Sheet.model_json_schema()

    # --- character model ---

    def new_character(self, spec: CharSpec, rng: Rng) -> SheetDict:
        d = spec.data
        stats = {**_DEFAULT_STATS, **(d.get("stats") or {})}
        return Sheet(
            stats=stats,
            harm=int(d.get("harm", 0)),
            conditions=list(d.get("conditions", [])),
            xp=int(d.get("xp", 0)),
        ).model_dump()

    def progression(self, sheet: SheetDict, award: Award) -> SheetDict:
        s = Sheet.model_validate(sheet)
        xp = s.xp + int(award.data.get("xp", 0))
        stats = dict(s.stats)
        # Advance by FAILING: every _XP_PER_ADVANCE marks, bump the lowest stat (deterministic:
        # lowest value, then name), capped at _STAT_CAP. This is the anti-d20 progression axis.
        while xp >= _XP_PER_ADVANCE:
            xp -= _XP_PER_ADVANCE
            low = min(stats, key=lambda k: (stats[k], k))
            stats[low] = min(_STAT_CAP, stats[low] + 1)
        return s.model_copy(update={"xp": xp, "stats": stats}).model_dump()

    # --- moves (free-roam checks) ---

    def affordances(self) -> list[Affordance]:
        return [
            Affordance(
                id="read_the_situation",
                stat="quick",
                trigger_categories=["search"],
                description="size up a charged moment",
            ),
            Affordance(
                id="persuade",
                stat="clever",
                trigger_categories=["change_disposition"],
                description="talk someone around with leverage",
            ),
            Affordance(
                id="keep_cool",
                stat="steady",
                trigger_categories=["coerce"],
                description="hold steady under pressure",
            ),
            Affordance(
                id="seize_by_force",
                stat="forceful",
                trigger_categories=["violence"],
                starts_encounter=True,
                description="take something by force — starts a conflict",
            ),
        ]

    def resolve_check(self, req: CheckRequest, rng: Rng) -> CheckResult:
        sheet = Sheet.model_validate(req.sheet)
        stat = req.stat or "steady"
        mod = sheet.stats.get(stat, 0)
        dice = rng.roll(2, 6)  # 2d6 — no DC, no advantage; the tiers are fixed and self-scaling
        total = dice + mod
        outcome = "miss" if total <= 6 else "partial" if total <= 9 else "full"
        label = {"miss": "miss", "partial": "partial (success at a cost)", "full": "full success"}[
            outcome
        ]
        trace = f"{stat} move: 2d6 ({dice}) {mod:+d} = {total} → {label}"
        return CheckResult(
            outcome=outcome, trace=trace, detail={"dice": dice, "stat": mod, "total": total}
        )

    # --- conflict (the move-exchange; no initiative, no rounds) ---

    def start_encounter(self, ctx: EncounterCtx, rng: Rng) -> EncounterState:
        # No initiative roll (the point): order is the combatants as handed in (aggressor first).
        fighters = {
            c.actor_id: _Fighter(
                actor_id=c.actor_id, team=c.team, sheet=Sheet.model_validate(c.sheet)
            )
            for c in ctx.combatants
        }
        order = [c.actor_id for c in ctx.combatants]
        turn_index = next((i for i, a in enumerate(order) if fighters[a].sheet.in_fight), 0)
        return _Encounter(
            encounter_id=ctx.encounter_id, fighters=fighters, order=order, turn_index=turn_index
        )

    def current_actor(self, state: EncounterState) -> str | None:
        st = _as_enc(state)
        if self.is_over(st) is not None:
            return None
        return st.order[st.turn_index]

    def sheets(self, state: EncounterState) -> dict[str, SheetDict]:
        return {a: f.sheet.model_dump() for a, f in _as_enc(state).fighters.items()}

    def legal_actions(self, state: EncounterState, actor_id: str) -> list[ActionSpec]:
        st = _as_enc(state)
        me = st.fighters[actor_id]
        foes = sorted(a for a, f in st.fighters.items() if f.team != me.team and f.sheet.in_fight)
        specs: list[ActionSpec] = []
        if foes:
            specs.append(ActionSpec(kind="seize_by_force", targets=foes))
        specs.append(ActionSpec(kind="keep_cool"))
        return specs

    def npc_action(self, state: EncounterState, actor_id: str, rng: Rng) -> Action:
        st = _as_enc(state)
        me = st.fighters[actor_id]
        foes = sorted(a for a, f in st.fighters.items() if f.team != me.team and f.sheet.in_fight)
        if not foes:
            return Action(kind="keep_cool", actor_id=actor_id)
        # Press the foe closest to going out (most harm, then name) — deterministic.
        target = max(foes, key=lambda a: (st.fighters[a].sheet.harm, a))
        return Action(kind="seize_by_force", actor_id=actor_id, target_id=target)

    def resolve_action(
        self, state: EncounterState, action: Action, rng: Rng
    ) -> tuple[EncounterState, list[Effect]]:
        st = _as_enc(state)
        fighters = {k: v.model_copy(deep=True) for k, v in st.fighters.items()}
        effects: list[Effect] = []
        actor = fighters[action.actor_id]

        if action.kind == "seize_by_force" and action.target_id is not None:
            target = fighters[action.target_id]
            mod = actor.sheet.stats.get("forceful", 0)
            dice = rng.roll(2, 6)
            total = dice + mod
            if total >= 10:  # full: hit hard, pay nothing
                target.sheet.harm = min(_HARM_MAX, target.sheet.harm + 2)
                effects.append(
                    Effect(
                        kind="full",
                        actor_id=target.actor_id,
                        payload={"harm": 2, "source": actor.actor_id},
                        trace=f"seize 2d6({dice}){mod:+d}={total} → full: 2 harm",
                    )
                )
            elif total >= 7:  # PARTIAL: you succeed AND take a cost — the d20-inexpressible band
                target.sheet.harm = min(_HARM_MAX, target.sheet.harm + 1)
                if _EXPOSED not in actor.sheet.conditions:
                    actor.sheet.conditions.append(_EXPOSED)
                effects.append(
                    Effect(
                        kind="partial",
                        actor_id=target.actor_id,
                        payload={
                            "harm": 1,
                            "cost_condition": _EXPOSED,
                            "cost_actor": actor.actor_id,
                        },
                        trace=f"seize {total}: partial — 1 harm, you are {_EXPOSED}",
                    )
                )
            else:  # miss: the foe makes a hard move against you
                actor.sheet.harm = min(_HARM_MAX, actor.sheet.harm + 2)
                effects.append(
                    Effect(
                        kind="miss",
                        actor_id=actor.actor_id,
                        payload={"harm": 2},
                        trace=f"seize 2d6({dice}){mod:+d}={total} → miss: take 2 harm",
                    )
                )
        # 'keep_cool' is a hold action with no mechanical effect in this minimal build.

        advanced = self._advance_turn(st.model_copy(update={"fighters": fighters}))
        return advanced, effects

    def is_over(self, state: EncounterState) -> EncounterOutcome | None:
        st = _as_enc(state)
        live_teams = {f.team for f in st.fighters.values() if f.sheet.in_fight}
        if len(live_teams) > 1:
            return None
        survivors = sorted(a for a, f in st.fighters.items() if f.sheet.in_fight)
        out = sorted(a for a, f in st.fighters.items() if not f.sheet.in_fight)
        winner = next(iter(live_teams)) if live_teams else None
        return EncounterOutcome(winner_team=winner, survivors=survivors, out_of_fight=out)

    def _advance_turn(self, st: _Encounter) -> _Encounter:
        if self.is_over(st) is not None:
            return st
        n = len(st.order)
        idx = st.turn_index
        for _ in range(n):
            idx = (idx + 1) % n
            if st.fighters[st.order[idx]].sheet.in_fight:
                break
        return st.model_copy(update={"turn_index": idx})


def _as_enc(state: EncounterState) -> _Encounter:
    return state if isinstance(state, _Encounter) else _Encounter.model_validate(state.model_dump())
