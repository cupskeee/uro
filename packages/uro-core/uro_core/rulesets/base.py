"""Ruleset PORT — the interface + data contracts (docs/06).

Mechanics live behind this port; the core ring (pipeline) imports only these types and
the `Ruleset` Protocol, never a concrete ruleset (import-linter bans `rulesets.uro_basic`).
Built-in "Uro Basic" implements it. Everything here is deterministic value data — rolls
come from the injected `Rng`, so a beat replays identically (docs/10). Contract discipline
mirrors the event catalog: these shapes are versioned and changed with the code that uses
them. Caveat (OQ-13): shaped against one d20 ruleset so far — d20 assumptions (ability
scores, HP/AC) may have leaked into the "generic" contract until an alien ruleset is tried.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from uro_core.rulesets.rng import Rng

Ability = Literal["STR", "DEX", "CON", "INT", "WIS", "CHA"]
ABILITIES: tuple[Ability, ...] = ("STR", "DEX", "CON", "INT", "WIS", "CHA")

# Difficulty tiers (docs/06). A plan/affordance names a tier; the ruleset maps it to a DC.
Difficulty = Literal["easy", "medium", "hard"]
DIFFICULTY_DC: dict[Difficulty, int] = {"easy": 10, "medium": 15, "hard": 20}


# --- character model ---


class Sheet(BaseModel):
    """A character sheet. `abilities` are the six scores; modifiers derive as (score-10)//2."""

    abilities: dict[str, int]
    max_hp: int
    hp: int
    ac: int
    level: int = Field(default=1, ge=1)
    proficiency: int = 2
    weapon_tier: int = Field(default=1, ge=1)  # damage-die tier: 1→d6, 2→d8, 3→d10

    def modifier(self, ability: str) -> int:
        return (self.abilities.get(ability, 10) - 10) // 2

    @property
    def conscious(self) -> bool:
        return self.hp > 0


class CharSpec(BaseModel):
    """Input to `new_character`: explicit ability scores (or ruleset defaults) + level/gear."""

    abilities: dict[str, int] | None = None
    level: int = Field(default=1, ge=1)
    weapon_tier: int = Field(default=1, ge=1)


class Award(BaseModel):
    """Input to `progression` — a minimal level-up award for the PoC."""

    levels: int = Field(default=0, ge=0)


# --- free-roam checks ---


class Affordance(BaseModel):
    """A mechanically-backed verb the planner may invoke (docs/06, D-21). `trigger_categories`
    are intent classes that MUST route through this affordance — plan validation enforces that
    deterministically GIVEN the planner's honest classification (docs/13); a misclassified
    intent can still slip through, and consequence gating (the backstop) is not built yet."""

    id: str
    ability: Ability
    difficulty: Difficulty = "medium"
    trigger_categories: list[str] = Field(default_factory=list)
    starts_encounter: bool = False
    description: str = ""


class CheckRequest(BaseModel):
    sheet: Sheet
    ability: Ability
    dc: int
    advantage: bool = False
    disadvantage: bool = False


class CheckResult(BaseModel):
    success: bool
    ability: str
    roll: int  # the d20 face used (after advantage/disadvantage)
    modifier: int
    total: int
    dc: int
    trace: str  # human-readable roll math for the narrator to weave in (docs/06)


# --- encounter mode ---


class Combatant(BaseModel):
    actor_id: str
    team: str  # e.g. "party" | "foes" — is_over checks whether a team is wiped
    sheet: Sheet
    initiative: int = 0


class EncounterCtx(BaseModel):
    """Everything `start_encounter` needs: the combatants (sheets + teams), pre-initiative."""

    encounter_id: str
    combatants: list[Combatant]


class EncounterState(BaseModel):
    """The turn-loop's serializable state (parkable for async/Chronicler resolution, D-25)."""

    encounter_id: str
    combatants: dict[str, Combatant]  # by actor_id, carrying current hp
    order: list[str]  # actor_ids in initiative order
    turn_index: int = 0
    round: int = 1
    over: bool = False

    def current_actor(self) -> str:
        return self.order[self.turn_index]


ActionKind = Literal["attack", "defend", "flee"]


class ActionSpec(BaseModel):
    kind: ActionKind
    targets: list[str] = Field(default_factory=list)  # legal target actor_ids


class Action(BaseModel):
    kind: ActionKind
    actor_id: str
    target_id: str | None = None


class Effect(BaseModel):
    """A mechanical outcome of an action. The pipeline maps each to an R-emitted domain
    event (ActorDamaged / ActorDied / …, docs/12) so mechanics are timeline citizens."""

    kind: Literal["damage", "death"]
    actor_id: str  # the affected actor
    amount: int = 0
    source: str = ""  # attacker actor_id or cause
    trace: str = ""


class EncounterOutcome(BaseModel):
    winner_team: str | None
    survivors: list[str]
    casualties: list[str]


class Ruleset(Protocol):
    """The mechanics plugin (docs/06). Deterministic: every random draw comes from `rng`."""

    id: str
    version: str

    def sheet_schema(self) -> dict[str, Any]: ...

    def new_character(self, spec: CharSpec, rng: Rng) -> Sheet: ...

    def progression(self, sheet: Sheet, award: Award) -> Sheet: ...

    def affordances(self) -> list[Affordance]: ...

    def dc_for(self, difficulty: Difficulty) -> int:
        """Map an affordance's difficulty tier to a numeric DC — the mechanics gate uses this
        to build a CheckRequest from an affordance without knowing the ruleset's DC scale."""
        ...

    def resolve_check(self, req: CheckRequest, rng: Rng) -> CheckResult: ...

    def start_encounter(self, ctx: EncounterCtx, rng: Rng) -> EncounterState: ...

    def legal_actions(self, state: EncounterState, actor_id: str) -> list[ActionSpec]: ...

    def npc_action(self, state: EncounterState, actor_id: str, rng: Rng) -> Action: ...

    def resolve_action(
        self, state: EncounterState, action: Action, rng: Rng
    ) -> tuple[EncounterState, list[Effect]]: ...

    def is_over(self, state: EncounterState) -> EncounterOutcome | None: ...
