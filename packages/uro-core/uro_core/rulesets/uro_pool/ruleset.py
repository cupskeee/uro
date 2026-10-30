"""Uro Pool — the third built-in ruleset: an ORIGINAL d6 dice-pool system. Pure, deterministic, no
LLM (docs/06). Like Uro PbtA (D-30), it exists to keep the ruleset port honest — this time on the
axes neither earlier ruleset touches:

    uro_basic (d20)            uro_pbta (2d6)              uro_pool (d6 pool)
    -----------------          ------------------------    --------------------------------------
    d20 + mod vs a DC          2d6 + stat vs fixed 7/10    roll TRAIT-many d6; difficulty sets the
                                                           TARGET each die must reach; count marks
    pass / fail                miss / partial / full       fail / success / TRIUMPH (3+ marks)
    —                          a partial costs you         a failed roll is PUSHED once — rerolled
                                                           at +1 strain, a standing resource
    hp countdown               a 0-4 harm clock            WOUNDS shrink every pool: harm degrades
                                                           what you can do, not a hit-point bar
    initiative order           alternating moves           SIMULTANEOUS exchanges: everyone commits,
                                                           then the round resolves at once (two
                                                           fighters can wound each other)
    levels from winning        XP from failing             MILESTONES from surviving encounters

Honest limit: the pipeline never applies a free-roam check's consequences to the sheet (true of
every ruleset — PbtA's XP-on-a-miss included), so a push during a CHECK is recorded in its detail
only.
A push during an ENCOUNTER changes the fighter's strain, which persists via the final SheetUpdated.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

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

TRAITS: tuple[str, ...] = ("might", "wits", "nerve")
_DEFAULT_TRAITS: dict[str, int] = {"might": 2, "wits": 2, "nerve": 2}
_TRAIT_CAP = 5
_WOUNDS_DOWN = 3  # at 3 wounds you are out of the fight
_STRAIN_LIMIT = 5  # at this much strain you can no longer push
_MILESTONES_PER_ADVANCE = 3
_TRIUMPH_MARKS = 3
_TARGETS: dict[str, int] = {"easy": 3, "": 4, "normal": 4, "hard": 5, "dire": 6}
_STRIKE_TARGET = 4  # attack and defence dice both mark on 4+
_MAX_WOUNDS_PER_STRIKE = 2


class Sheet(BaseModel):
    """Uro Pool's OWN sheet — no hp, no harm clock. Traits are dice counts; wounds shrink every
    pool; strain is the toll of pushing; milestones accrue from surviving encounters."""

    traits: dict[str, int]
    wounds: int = 0
    strain: int = 0
    milestones: int = 0

    def pool(self, trait: str) -> int:
        """Dice rolled for `trait`: its rating minus wounds, never below one die."""
        return max(1, self.traits.get(trait, 1) - self.wounds)

    @property
    def in_fight(self) -> bool:
        return self.wounds < _WOUNDS_DOWN

    @property
    def can_push(self) -> bool:
        return self.strain < _STRAIN_LIMIT


class _Fighter(BaseModel):
    actor_id: str
    team: str
    sheet: Sheet


class _Encounter(EncounterState):
    """Uro Pool's private exchange state. Every standing fighter COMMITS an action each round
    (`current_actor` walks those who haven't); the round resolves only when the last one commits —
    so most actions produce no effects, and the runner must not assume one effect per action."""

    fighters: dict[str, _Fighter]
    order: list[str]
    committed: dict[str, Action] = Field(default_factory=dict)
    round: int = 1


def _marks(dice: list[int], target: int) -> int:
    return sum(1 for d in dice if d >= target)


def _roll_pool(sheet: Sheet, trait: str, target: int, rng: Rng) -> tuple[list[int], bool]:
    """Roll `trait`'s pool; on zero marks, PUSH once (reroll it all) if strain allows. Returns the
    dice that count and whether a push happened (the caller charges the strain)."""
    dice = [rng.die(6) for _ in range(sheet.pool(trait))]
    if _marks(dice, target) == 0 and sheet.can_push:
        return [rng.die(6) for _ in range(sheet.pool(trait))], True
    return dice, False


class UroPool:
    id = "uro-pool"
    version = "0"

    def sheet_schema(self) -> dict[str, Any]:
        return Sheet.model_json_schema()

    # --- character model ---

    def new_character(self, spec: CharSpec, rng: Rng) -> SheetDict:
        d = spec.data
        return Sheet(
            traits={**_DEFAULT_TRAITS, **(d.get("traits") or {})},
            wounds=int(d.get("wounds", 0)),
            strain=int(d.get("strain", 0)),
            milestones=int(d.get("milestones", 0)),
        ).model_dump()

    def progression(self, sheet: SheetDict, award: Award) -> SheetDict:
        s = Sheet.model_validate(sheet)
        milestones = s.milestones + int(award.data.get("milestone", 0))
        traits = dict(s.traits)
        # Every _MILESTONES_PER_ADVANCE survived encounters adds a die to the weakest trait
        # (deterministic: lowest rating, then name), capped at _TRAIT_CAP.
        while milestones >= _MILESTONES_PER_ADVANCE:
            milestones -= _MILESTONES_PER_ADVANCE
            low = min(traits, key=lambda k: (traits[k], k))
            traits[low] = min(_TRAIT_CAP, traits[low] + 1)
        strain = 0 if award.data.get("rest") else s.strain  # rest clears the toll of pushing
        return s.model_copy(
            update={"milestones": milestones, "traits": traits, "strain": strain}
        ).model_dump()

    # --- free-roam checks ---

    def affordances(self) -> list[Affordance]:
        return [
            Affordance(
                id="investigate",
                stat="wits",
                trigger_categories=["search"],
                description="search for what's hidden or wrong",
            ),
            Affordance(
                id="talk_down",
                stat="nerve",
                trigger_categories=["change_disposition"],
                description="steady someone and change their mind",
            ),
            Affordance(
                id="hold_firm",
                stat="nerve",
                trigger_categories=["coerce"],
                description="refuse to yield under pressure",
            ),
            Affordance(
                id="strike",
                stat="might",
                trigger_categories=["violence"],
                starts_encounter=True,
                description="lash out — starts an exchange",
            ),
        ]

    def resolve_check(self, req: CheckRequest, rng: Rng) -> CheckResult:
        sheet = Sheet.model_validate(req.sheet)
        trait = req.stat or "nerve"
        target = _TARGETS.get(req.difficulty, _TARGETS[""])
        dice, pushed = _roll_pool(sheet, trait, target, rng)
        marks = _marks(dice, target)
        outcome = "fail" if marks == 0 else "triumph" if marks >= _TRIUMPH_MARKS else "success"
        push_note = " (pushed: +1 strain)" if pushed else ""
        trace = f"{trait} pool {sorted(dice)} vs {target}+ → {marks} mark(s): {outcome}{push_note}"
        return CheckResult(
            outcome=outcome,
            trace=trace,
            detail={"dice": dice, "target": target, "marks": marks, "pushed": pushed},
        )

    # --- encounters: simultaneous exchanges ---

    def start_encounter(self, ctx: EncounterCtx, rng: Rng) -> EncounterState:
        fighters = {
            c.actor_id: _Fighter(
                actor_id=c.actor_id, team=c.team, sheet=Sheet.model_validate(c.sheet)
            )
            for c in ctx.combatants
        }
        return _Encounter(
            encounter_id=ctx.encounter_id,
            fighters=fighters,
            order=[c.actor_id for c in ctx.combatants],
        )

    def current_actor(self, state: EncounterState) -> str | None:
        st = _as_enc(state)
        if self.is_over(st) is not None:
            return None
        return next(
            (a for a in st.order if st.fighters[a].sheet.in_fight and a not in st.committed), None
        )

    def sheets(self, state: EncounterState) -> dict[str, SheetDict]:
        return {a: f.sheet.model_dump() for a, f in _as_enc(state).fighters.items()}

    def legal_actions(self, state: EncounterState, actor_id: str) -> list[ActionSpec]:
        foes = _foes(_as_enc(state), actor_id)
        specs = [ActionSpec(kind="strike", targets=foes)] if foes else []
        return [*specs, ActionSpec(kind="brace")]

    def npc_action(self, state: EncounterState, actor_id: str, rng: Rng) -> Action:
        st = _as_enc(state)
        foes = _foes(st, actor_id)
        if not foes:
            return Action(kind="brace", actor_id=actor_id)
        # Press the foe nearest to going down (most wounds, then name) — deterministic.
        target = max(foes, key=lambda a: (st.fighters[a].sheet.wounds, a))
        return Action(kind="strike", actor_id=actor_id, target_id=target)

    def resolve_action(
        self, state: EncounterState, action: Action, rng: Rng
    ) -> tuple[EncounterState, list[Effect]]:
        st = _as_enc(state)
        committed = {**st.committed, action.actor_id: action}
        standing = [a for a in st.order if st.fighters[a].sheet.in_fight]
        if any(a not in committed for a in standing):  # the round isn't closed yet — no effects
            return st.model_copy(update={"committed": committed}), []
        return self._resolve_round(st, committed, rng)

    def _resolve_round(
        self, st: _Encounter, committed: dict[str, Action], rng: Rng
    ) -> tuple[EncounterState, list[Effect]]:
        """Resolve every committed strike against the fighters AS THEY STOOD at the round's start,
        then apply all wounds at once — two fighters can wound each other in one exchange."""
        fighters = {k: v.model_copy(deep=True) for k, v in st.fighters.items()}
        wounds: dict[str, int] = {}
        effects: list[Effect] = []
        braced = {a for a, act in committed.items() if act.kind == "brace"}
        for attacker_id in st.order:  # strikers in commit order — deterministic dice order
            act = committed.get(attacker_id)
            if act is None or act.kind != "strike" or act.target_id not in st.fighters:
                continue
            attacker, target = st.fighters[attacker_id].sheet, st.fighters[act.target_id].sheet
            att, pushed = _roll_pool(attacker, "might", _STRIKE_TARGET, rng)
            if pushed:
                fighters[attacker_id].sheet.strain += 1
            defence = target.pool("nerve") + (1 if act.target_id in braced else 0)
            dfn = [rng.die(6) for _ in range(defence)]
            net = _marks(att, _STRIKE_TARGET) - _marks(dfn, _STRIKE_TARGET)
            dealt = max(0, min(_MAX_WOUNDS_PER_STRIKE, net))
            wounds[act.target_id] = wounds.get(act.target_id, 0) + dealt
            push_note = ", pushed" if pushed else ""
            effects.append(
                Effect(
                    kind="wound" if dealt else "parried",
                    actor_id=act.target_id,
                    payload={"wounds": dealt, "source": attacker_id, "pushed": pushed},
                    trace=f"round {st.round}: {attacker_id} strikes {act.target_id} "
                    f"{sorted(att)} vs {sorted(dfn)}{push_note} → {dealt} wound(s)",
                )
            )
        for actor_id, dealt in wounds.items():  # simultaneous: applied only after every roll
            sheet = fighters[actor_id].sheet
            sheet.wounds = min(_WOUNDS_DOWN, sheet.wounds + dealt)
        nxt = st.model_copy(update={"fighters": fighters, "committed": {}, "round": st.round + 1})
        return nxt, effects

    def is_over(self, state: EncounterState) -> EncounterOutcome | None:
        st = _as_enc(state)
        live_teams = {f.team for f in st.fighters.values() if f.sheet.in_fight}
        if len(live_teams) > 1:
            return None
        survivors = sorted(a for a, f in st.fighters.items() if f.sheet.in_fight)
        out = sorted(a for a, f in st.fighters.items() if not f.sheet.in_fight)
        winner = next(iter(live_teams)) if live_teams else None
        return EncounterOutcome(winner_team=winner, survivors=survivors, out_of_fight=out)


def _foes(st: _Encounter, actor_id: str) -> list[str]:
    me = st.fighters[actor_id]
    return sorted(a for a, f in st.fighters.items() if f.team != me.team and f.sheet.in_fight)


def _as_enc(state: EncounterState) -> _Encounter:
    return state if isinstance(state, _Encounter) else _Encounter.model_validate(state.model_dump())
