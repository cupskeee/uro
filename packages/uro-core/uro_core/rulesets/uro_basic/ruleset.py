"""Built-in "Uro Basic" ruleset — a minimal, original d20-flavored system (docs/06, D-10).

Concrete impl: the core ring imports the ruleset PORT (`rulesets.base`), never this module
(import-linter, docs/14). Deterministic throughout — every roll comes from the injected `Rng`,
so encounters replay identically (docs/10). Small on purpose: enough to exercise every port
method (checks, initiative, attacks, damage, death, progression), not a full RPG.

D-30 note: ALL d20 vocabulary lives here, not in the port — the six ability scores, the
(score-10)//2 modifier, hp/ac, the DC scale, initiative/rounds, weapon-damage tiers, and
advance-by-levelling. The generic port knows none of it; `uro_pbta` is the structurally
different sibling that proves the port stayed clean (OQ-13).
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

ABILITIES: tuple[str, ...] = ("STR", "DEX", "CON", "INT", "WIS", "CHA")
_DIFFICULTY_DC: dict[str, int] = {"easy": 10, "medium": 15, "hard": 20}
_DEFAULT_DC = 15
_DEFAULT_ABILITIES: dict[str, int] = {
    "STR": 12,
    "DEX": 12,
    "CON": 12,
    "INT": 10,
    "WIS": 10,
    "CHA": 10,
}
_WEAPON_DIE: dict[int, int] = {1: 6, 2: 8, 3: 10}  # weapon_tier → damage die faces
_MAX_LEVEL = 5


class Sheet(BaseModel):
    """Uro Basic's OWN character sheet (a d20 shape — abilities + hp/ac). The generic port holds
    only an opaque dict; this is the concrete model uro_basic validates it into and back out of."""

    abilities: dict[str, int]
    max_hp: int
    hp: int
    ac: int
    level: int = 1
    proficiency: int = 2
    weapon_tier: int = 1  # damage-die tier: 1→d6, 2→d8, 3→d10

    def modifier(self, ability: str) -> int:
        return (self.abilities.get(ability, 10) - 10) // 2

    @property
    def conscious(self) -> bool:
        return self.hp > 0


class _Fighter(BaseModel):
    """A combatant inside uro_basic's private encounter state — a TYPED sheet + initiative."""

    actor_id: str
    team: str
    sheet: Sheet
    initiative: int = 0


class _Encounter(EncounterState):
    """Uro Basic's private encounter state: d20 initiative order + rounds (the port's
    EncounterState is opaque; these fields live only here)."""

    fighters: dict[str, _Fighter]
    order: list[str]
    turn_index: int = 0
    round: int = 1


def _hp_for_level(con_mod: int, level: int) -> int:
    """Base 8+CON at L1, then (5+CON) per level after. Never below 1."""
    return max(1, (8 + con_mod) + (level - 1) * (5 + con_mod))


def _damage_die(weapon_tier: int) -> int:
    return _WEAPON_DIE.get(weapon_tier, _WEAPON_DIE[max(_WEAPON_DIE)])


class UroBasic:
    id = "uro-basic"
    version = "0"

    def sheet_schema(self) -> dict[str, Any]:
        return Sheet.model_json_schema()

    # --- character model ---

    def new_character(self, spec: CharSpec, rng: Rng) -> SheetDict:
        # Uro Basic is a level 1-5 system (docs/06); clamp here so a high-level spec can't mint an
        # off-spec sheet — the level ceiling is Uro Basic's rule, NOT in the generic port (OQ-13).
        d = spec.data
        level = min(_MAX_LEVEL, int(d.get("level", 1)))
        spec_abilities = d.get("abilities")
        abilities = (
            {**_DEFAULT_ABILITIES, **spec_abilities} if spec_abilities else dict(_DEFAULT_ABILITIES)
        )
        con_mod = (abilities.get("CON", 10) - 10) // 2
        dex_mod = (abilities.get("DEX", 10) - 10) // 2
        max_hp = _hp_for_level(con_mod, level)
        return Sheet(
            abilities=abilities,
            max_hp=max_hp,
            hp=max_hp,
            ac=10 + dex_mod,
            level=level,
            proficiency=2,  # flat across levels 1-5 (docs/06)
            weapon_tier=int(d.get("weapon_tier", 1)),
        ).model_dump()

    def progression(self, sheet: SheetDict, award: Award) -> SheetDict:
        s = Sheet.model_validate(sheet)
        new_level = min(_MAX_LEVEL, s.level + int(award.data.get("levels", 0)))
        max_hp = _hp_for_level(s.modifier("CON"), new_level)
        # Only an ACTUAL level-up restores to full HP; a no-op or capped award must not heal
        # (just re-clamp current HP under the possibly-changed max).
        hp = max_hp if new_level > s.level else min(s.hp, max_hp)
        return s.model_copy(update={"level": new_level, "max_hp": max_hp, "hp": hp}).model_dump()

    # --- free-roam checks ---

    def affordances(self) -> list[Affordance]:
        return [
            Affordance(
                id="persuade",
                stat="CHA",
                trigger_categories=["change_disposition"],
                description="talk someone toward your intent",
            ),
            Affordance(
                id="intimidate",
                stat="CHA",
                trigger_categories=["coerce"],
                description="frighten someone into compliance",
            ),
            Affordance(
                id="sneak",
                stat="DEX",
                trigger_categories=["stealth"],
                description="move or act unseen",
            ),
            Affordance(
                id="perceive",
                stat="WIS",
                difficulty="easy",
                trigger_categories=["search"],
                description="notice what is hidden",
            ),
            Affordance(
                id="attack",
                stat="STR",
                trigger_categories=["violence"],
                starts_encounter=True,
                description="strike at someone — starts an encounter",
            ),
        ]

    def resolve_check(self, req: CheckRequest, rng: Rng) -> CheckResult:
        sheet = Sheet.model_validate(req.sheet)
        ability = req.stat or "STR"
        dc = _DIFFICULTY_DC.get(req.difficulty, _DEFAULT_DC)
        advantage = bool(req.modifiers.get("advantage"))
        disadvantage = bool(req.modifiers.get("disadvantage"))
        if advantage and not disadvantage:
            roll = max(rng.d20(), rng.d20())
        elif disadvantage and not advantage:
            roll = min(rng.d20(), rng.d20())
        else:
            roll = rng.d20()
        modifier = sheet.modifier(ability)
        total = roll + modifier
        success = total >= dc
        trace = (
            f"{ability} check: d20 ({roll}) {modifier:+d} = {total} vs DC {dc} "
            f"→ {'success' if success else 'failure'}"
        )
        return CheckResult(
            outcome="success" if success else "failure",
            trace=trace,
            detail={"roll": roll, "modifier": modifier, "total": total, "dc": dc},
        )

    # --- encounter mode ---

    def start_encounter(self, ctx: EncounterCtx, rng: Rng) -> EncounterState:
        fighters: dict[str, _Fighter] = {}
        # Roll initiative in a STABLE order (by actor_id) so the seed alone fixes the rolls; each
        # ruleset owns its combatants' sheets (validated from the port's opaque dict).
        for c in sorted(ctx.combatants, key=lambda c: c.actor_id):
            sheet = Sheet.model_validate(c.sheet)
            init = rng.d20() + sheet.modifier("DEX")
            fighters[c.actor_id] = _Fighter(
                actor_id=c.actor_id, team=c.team, sheet=sheet, initiative=init
            )
        order = sorted(fighters, key=lambda a: (-fighters[a].initiative, a))
        # Seat the first turn on a conscious combatant (mirrors _advance_turn); one who enters
        # already downed never gets to act. Fallback 0 only in the degenerate all-downed case.
        turn_index = next((i for i, a in enumerate(order) if fighters[a].sheet.conscious), 0)
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
        foes = sorted(a for a, f in st.fighters.items() if f.team != me.team and f.sheet.conscious)
        specs: list[ActionSpec] = []
        if foes:
            specs.append(ActionSpec(kind="attack", targets=foes))
        specs.append(ActionSpec(kind="defend"))
        specs.append(ActionSpec(kind="flee"))
        return specs

    def npc_action(self, state: EncounterState, actor_id: str, rng: Rng) -> Action:
        st = _as_enc(state)
        me = st.fighters[actor_id]
        foes = sorted(a for a, f in st.fighters.items() if f.team != me.team and f.sheet.conscious)
        if not foes:
            return Action(kind="defend", actor_id=actor_id)
        # Deterministic focus-fire: the weakest conscious foe (lowest hp, then actor_id).
        target = min(foes, key=lambda a: (st.fighters[a].sheet.hp, a))
        return Action(kind="attack", actor_id=actor_id, target_id=target)

    def resolve_action(
        self, state: EncounterState, action: Action, rng: Rng
    ) -> tuple[EncounterState, list[Effect]]:
        st = _as_enc(state)
        fighters = {k: v.model_copy(deep=True) for k, v in st.fighters.items()}
        effects: list[Effect] = []
        actor = fighters[action.actor_id]

        if action.kind == "attack" and action.target_id is not None:
            target = fighters[action.target_id]
            atk = rng.d20() + actor.sheet.modifier("STR") + actor.sheet.proficiency
            if atk >= target.sheet.ac:
                dmg = max(
                    1, rng.die(_damage_die(actor.sheet.weapon_tier)) + actor.sheet.modifier("STR")
                )
                target.sheet.hp = max(0, target.sheet.hp - dmg)
                effects.append(
                    Effect(
                        kind="hit",
                        actor_id=target.actor_id,
                        payload={"amount": dmg, "source": actor.actor_id},
                        trace=f"attack {atk} vs AC {target.sheet.ac} → hit for {dmg}",
                    )
                )
                if not target.sheet.conscious:
                    effects.append(
                        Effect(
                            kind="down",
                            actor_id=target.actor_id,
                            payload={"source": actor.actor_id},
                            trace="struck down",
                        )
                    )
            else:
                effects.append(
                    Effect(
                        kind="miss",
                        actor_id=target.actor_id,
                        payload={"source": actor.actor_id},
                        trace=f"attack {atk} vs AC {target.sheet.ac} → miss",
                    )
                )
        # 'defend' and 'flee' produce no mechanical effect in this minimal ruleset; the narration
        # handles their color, and 'flee' as a full disengage is future work.

        advanced = self._advance_turn(st.model_copy(update={"fighters": fighters}))
        return advanced, effects

    def is_over(self, state: EncounterState) -> EncounterOutcome | None:
        st = _as_enc(state)
        live_teams = {f.team for f in st.fighters.values() if f.sheet.conscious}
        if len(live_teams) > 1:
            return None
        survivors = sorted(a for a, f in st.fighters.items() if f.sheet.conscious)
        out = sorted(a for a, f in st.fighters.items() if not f.sheet.conscious)
        winner = next(iter(live_teams)) if live_teams else None
        return EncounterOutcome(winner_team=winner, survivors=survivors, out_of_fight=out)

    def _advance_turn(self, st: _Encounter) -> _Encounter:
        """Advance to the next conscious combatant; wrap → next round. If the encounter is
        already decided, mark it over instead of advancing."""
        if self.is_over(st) is not None:
            return st
        n = len(st.order)
        idx, rnd = st.turn_index, st.round
        for _ in range(n):
            idx += 1
            if idx >= n:
                idx, rnd = 0, rnd + 1
            if st.fighters[st.order[idx]].sheet.conscious:
                break
        return st.model_copy(update={"turn_index": idx, "round": rnd})


def _as_enc(state: EncounterState) -> _Encounter:
    """Narrow the opaque port state back to uro_basic's own encounter type."""
    return state if isinstance(state, _Encounter) else _Encounter.model_validate(state.model_dump())
