"""Built-in "Uro Basic" ruleset — a minimal, original d20-flavored system (docs/06, D-10).

Concrete impl: the core ring imports the ruleset PORT (`rulesets.base`), never this module
(import-linter, docs/14). Deterministic throughout — every roll comes from the injected
`Rng`, so encounters replay identically (docs/10). Small on purpose: enough to exercise
every port method (checks, initiative, attacks, damage, death, progression), not a full RPG.
"""

from __future__ import annotations

from typing import Any

from uro_core.rulesets.base import (
    DIFFICULTY_DC,
    Action,
    ActionSpec,
    Affordance,
    Award,
    CharSpec,
    CheckRequest,
    CheckResult,
    Combatant,
    Difficulty,
    Effect,
    EncounterCtx,
    EncounterOutcome,
    EncounterState,
    Sheet,
)
from uro_core.rulesets.rng import Rng

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

    def new_character(self, spec: CharSpec, rng: Rng) -> Sheet:
        # Uro Basic is a level 1-5 system (docs/06); clamp here so a high-level spec can't
        # mint an off-spec sheet — the level ceiling is Uro Basic's rule, NOT baked into the
        # generic port's Sheet/CharSpec (a different ruleset may have a different range, OQ-13).
        level = min(_MAX_LEVEL, spec.level)
        # A partial spec fills in from defaults, so a sheet always carries all six abilities.
        abilities = (
            {**_DEFAULT_ABILITIES, **spec.abilities} if spec.abilities else dict(_DEFAULT_ABILITIES)
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
            weapon_tier=spec.weapon_tier,
        )

    def progression(self, sheet: Sheet, award: Award) -> Sheet:
        new_level = min(_MAX_LEVEL, sheet.level + award.levels)
        max_hp = _hp_for_level(sheet.modifier("CON"), new_level)
        # Only an ACTUAL level-up restores to full HP; a no-op or capped award must not heal
        # (just re-clamp current HP under the possibly-changed max).
        hp = max_hp if new_level > sheet.level else min(sheet.hp, max_hp)
        return sheet.model_copy(update={"level": new_level, "max_hp": max_hp, "hp": hp})

    # --- free-roam checks ---

    def affordances(self) -> list[Affordance]:
        return [
            Affordance(
                id="persuade",
                ability="CHA",
                trigger_categories=["change_disposition"],
                description="talk someone toward your intent",
            ),
            Affordance(
                id="intimidate",
                ability="CHA",
                trigger_categories=["coerce"],
                description="frighten someone into compliance",
            ),
            Affordance(
                id="sneak",
                ability="DEX",
                trigger_categories=["stealth"],
                description="move or act unseen",
            ),
            Affordance(
                id="perceive",
                ability="WIS",
                difficulty="easy",
                trigger_categories=["search"],
                description="notice what is hidden",
            ),
            Affordance(
                id="attack",
                ability="STR",
                trigger_categories=["violence"],
                starts_encounter=True,
                description="strike at someone — starts an encounter",
            ),
        ]

    def resolve_check(self, req: CheckRequest, rng: Rng) -> CheckResult:
        if req.advantage and not req.disadvantage:
            a, b = rng.d20(), rng.d20()
            roll = max(a, b)
        elif req.disadvantage and not req.advantage:
            a, b = rng.d20(), rng.d20()
            roll = min(a, b)
        else:
            roll = rng.d20()
        modifier = req.sheet.modifier(req.ability)
        total = roll + modifier
        success = total >= req.dc
        trace = (
            f"{req.ability} check: d20 ({roll}) {modifier:+d} = {total} vs DC {req.dc} "
            f"→ {'success' if success else 'failure'}"
        )
        return CheckResult(
            success=success,
            ability=req.ability,
            roll=roll,
            modifier=modifier,
            total=total,
            dc=req.dc,
            trace=trace,
        )

    def dc_for(self, difficulty: Difficulty) -> int:
        """Convenience for the mechanics gate (3.2): affordance difficulty tier → DC."""
        return DIFFICULTY_DC.get(difficulty, DIFFICULTY_DC["medium"])

    # --- encounter mode ---

    def start_encounter(self, ctx: EncounterCtx, rng: Rng) -> EncounterState:
        combatants: dict[str, Combatant] = {}
        # Roll initiative in a STABLE order (by actor_id) so the seed alone fixes the rolls.
        # deep=True so the encounter state owns its sheets — mutating hp mid-fight never leaks
        # back into the caller's EncounterCtx (resolve_action also deep-copies before mutating).
        for c in sorted(ctx.combatants, key=lambda c: c.actor_id):
            init = rng.d20() + c.sheet.modifier("DEX")
            combatants[c.actor_id] = c.model_copy(update={"initiative": init}, deep=True)
        order = sorted(combatants, key=lambda a: (-combatants[a].initiative, a))
        # Seat the first turn on a conscious combatant (mirrors _advance_turn); one who enters
        # already downed never gets to act. Fallback 0 only in the degenerate all-downed case.
        turn_index = next((i for i, a in enumerate(order) if combatants[a].sheet.conscious), 0)
        return EncounterState(
            encounter_id=ctx.encounter_id,
            combatants=combatants,
            order=order,
            turn_index=turn_index,
            round=1,
        )

    def legal_actions(self, state: EncounterState, actor_id: str) -> list[ActionSpec]:
        me = state.combatants[actor_id]
        foes = sorted(
            a for a, c in state.combatants.items() if c.team != me.team and c.sheet.conscious
        )
        specs: list[ActionSpec] = []
        if foes:
            specs.append(ActionSpec(kind="attack", targets=foes))
        specs.append(ActionSpec(kind="defend"))
        specs.append(ActionSpec(kind="flee"))
        return specs

    def npc_action(self, state: EncounterState, actor_id: str, rng: Rng) -> Action:
        me = state.combatants[actor_id]
        foes = sorted(
            a for a, c in state.combatants.items() if c.team != me.team and c.sheet.conscious
        )
        if not foes:
            return Action(kind="defend", actor_id=actor_id)
        # Deterministic focus-fire: the weakest conscious foe (lowest hp, then actor_id).
        target = min(foes, key=lambda a: (state.combatants[a].sheet.hp, a))
        return Action(kind="attack", actor_id=actor_id, target_id=target)

    def resolve_action(
        self, state: EncounterState, action: Action, rng: Rng
    ) -> tuple[EncounterState, list[Effect]]:
        combatants = {k: v.model_copy(deep=True) for k, v in state.combatants.items()}
        effects: list[Effect] = []
        actor = combatants[action.actor_id]

        if action.kind == "attack" and action.target_id is not None:
            target = combatants[action.target_id]
            atk = rng.d20() + actor.sheet.modifier("STR") + actor.sheet.proficiency
            if atk >= target.sheet.ac:
                dmg = max(
                    1, rng.die(_damage_die(actor.sheet.weapon_tier)) + actor.sheet.modifier("STR")
                )
                target.sheet.hp = max(0, target.sheet.hp - dmg)
                effects.append(
                    Effect(
                        kind="damage",
                        actor_id=target.actor_id,
                        amount=dmg,
                        source=actor.actor_id,
                        trace=f"attack {atk} vs AC {target.sheet.ac} → hit for {dmg}",
                    )
                )
                if not target.sheet.conscious:
                    effects.append(
                        Effect(
                            kind="death",
                            actor_id=target.actor_id,
                            source=actor.actor_id,
                            trace="struck down",
                        )
                    )
            else:
                effects.append(
                    Effect(
                        kind="damage",
                        actor_id=target.actor_id,
                        amount=0,
                        source=actor.actor_id,
                        trace=f"attack {atk} vs AC {target.sheet.ac} → miss",
                    )
                )
        # 'defend' and 'flee' produce no mechanical effect in this minimal ruleset; the
        # narration handles their color, and 'flee' as a full disengage is future work.

        advanced = self._advance_turn(state.model_copy(update={"combatants": combatants}))
        return advanced, effects

    def is_over(self, state: EncounterState) -> EncounterOutcome | None:
        live_teams = {c.team for c in state.combatants.values() if c.sheet.conscious}
        if len(live_teams) > 1:
            return None
        survivors = sorted(a for a, c in state.combatants.items() if c.sheet.conscious)
        casualties = sorted(a for a, c in state.combatants.items() if not c.sheet.conscious)
        winner = next(iter(live_teams)) if live_teams else None
        return EncounterOutcome(winner_team=winner, survivors=survivors, casualties=casualties)

    def _advance_turn(self, state: EncounterState) -> EncounterState:
        """Advance to the next conscious combatant; wrap → next round. If the encounter is
        already decided, mark it over instead of advancing."""
        if self.is_over(state) is not None:
            return state.model_copy(update={"over": True})
        n = len(state.order)
        idx, rnd = state.turn_index, state.round
        for _ in range(n):
            idx += 1
            if idx >= n:
                idx, rnd = 0, rnd + 1
            if state.combatants[state.order[idx]].sheet.conscious:
                break
        return state.model_copy(update={"turn_index": idx, "round": rnd})
