"""Ruleset PORT — the interface + data contracts (docs/06).

Mechanics live behind this port; the core ring (pipeline) imports only these types and
the `Ruleset` Protocol, never a concrete ruleset (import-linter bans `rulesets.uro_basic`,
`rulesets.uro_pbta`). Everything here is deterministic value data — rolls come from the
injected `Rng`, so a beat replays identically (docs/10).

GAME-AGNOSTIC BY CONSTRUCTION (OQ-13 → D-30). This port names NO game vocabulary: no
abilities, no hit points, no armour class, no DC, no attack/defend, no damage. A character
SHEET is an opaque `dict` whose shape each ruleset owns and validates internally (the same way
the store/projector already persist it, docs/07); a check yields a ruleset-declared graded
`outcome` string (d20 says {failure,success}; a PbtA 2d6 system says {miss,partial,full} — the
7-9 partial that a `bool` could not hold); encounter STATE is an opaque ruleset subclass the
runner never introspects. Two structurally-different built-ins prove it: `uro_basic` (d20:
ability scores, hp/ac, initiative attrition) and `uro_pbta` (PbtA: 4 stats, a harm clock,
move exchanges). Any d20 word appearing here again is a regression — it belongs in a ruleset.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from uro_core.rulesets.rng import Rng

# A character sheet is OPAQUE to the core: an arbitrary JSON object whose shape the bound
# ruleset defines and validates. The port neither reads nor writes its fields.
Sheet = dict[str, Any]


# --- character model ---


class CharSpec(BaseModel):
    """Ruleset-opaque character-creation input. The ruleset interprets `data` (uro_basic reads
    `abilities`/`level`/`weapon_tier`; uro_pbta reads its stat spread + playbook)."""

    data: dict[str, Any] = Field(default_factory=dict)


class Award(BaseModel):
    """Ruleset-opaque advancement input — the port fixes NO advancement vocabulary. d20 sends
    `{"levels": 1}` (advance by winning); PbtA sends `{"xp": 1}` (mark XP on a miss — advance by
    failing). Neither is privileged by the port."""

    data: dict[str, Any] = Field(default_factory=dict)


# --- free-roam checks ---


class Affordance(BaseModel):
    """A mechanically-backed verb the planner may invoke (docs/06, D-21). `stat` is a
    ruleset-defined key (NOT a fixed ability enum); `difficulty` is a ruleset-opaque hint the
    ruleset resolves however it likes (uro_basic: easy/medium/hard→DC; uro_pbta: ignored, 2d6 is
    self-scaling). `trigger_categories` are intent classes that MUST route through this affordance
    — plan validation enforces that deterministically given the planner's honest classification
    (docs/13); a misclassified intent can still slip through (consequence gating not built)."""

    id: str
    stat: str = ""  # ruleset stat key the check rolls against ("" → ruleset default)
    difficulty: str = ""  # ruleset-opaque difficulty hint
    trigger_categories: list[str] = Field(default_factory=list)
    starts_encounter: bool = False
    description: str = ""


class CheckRequest(BaseModel):
    """Input to `resolve_check`, ruleset-neutral: the opaque sheet, the stat key, the opaque
    difficulty hint, and a `modifiers` bag for ruleset-specific knobs (uro_basic reads
    `advantage`/`disadvantage`). The ruleset owns ALL resolution — there is no DC here."""

    sheet: Sheet
    stat: str = ""
    difficulty: str = ""
    modifiers: dict[str, Any] = Field(default_factory=dict)


class CheckResult(BaseModel):
    """A resolved check. `outcome` is a ruleset-declared graded band (the generalization that
    replaced d20's binary `success: bool` — so a 2d6 system's miss/partial/full survives). The
    pipeline reads only `trace` (→ the narrator); `detail` carries ruleset specifics (the roll,
    the DC, the 2d6 sum…) for debugging without shaping the contract."""

    outcome: str
    trace: str
    detail: dict[str, Any] = Field(default_factory=dict)


# --- encounter mode ---


class Combatant(BaseModel):
    """A participant handed to `start_encounter` — actor id, team, and the opaque sheet. No
    initiative/hp here: how (or whether) turns are ordered is ruleset-internal state."""

    actor_id: str
    team: str  # e.g. "party" | "foes"; the ruleset's is_over decides what a decided fight means
    sheet: Sheet


class EncounterCtx(BaseModel):
    encounter_id: str
    combatants: list[Combatant]


class EncounterState(BaseModel):
    """OPAQUE, ruleset-owned turn/exchange state. The port carries only the id; each ruleset
    SUBCLASSES this with whatever it needs — uro_basic adds combatants/order/round/turn_index
    (initiative attrition); uro_pbta adds its move-exchange bookkeeping (no rounds). The runner
    (pipeline/encounter.py) NEVER introspects it: it advances the fight only through
    `current_actor`/`npc_action`/`resolve_action`/`is_over`/`sheets`. Was a d20-shaped struct
    (order/turn_index/round) before D-30."""

    model_config = ConfigDict(extra="allow")

    encounter_id: str


class ActionSpec(BaseModel):
    """A legal action offered on an actor's turn. `kind` is a ruleset-defined verb string (d20:
    attack/defend/flee; PbtA: a move name)."""

    kind: str
    targets: list[str] = Field(default_factory=list)


class Action(BaseModel):
    kind: str
    actor_id: str
    target_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)  # ruleset extras


class Effect(BaseModel):
    """A mechanical outcome of an action, ruleset-shaped. `kind` is ruleset-defined (uro_basic:
    damage/death; uro_pbta: condition/harm). The runner does NOT map effects to typed events —
    harm reaches the timeline through the ruleset-computed final SHEET (a `SheetUpdated`), so the
    port never assumes an hp scalar. Effects survive only as the human-readable `trace` and a
    coarse `result` label on `EncounterTurnTaken`."""

    kind: str
    actor_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    trace: str = ""


class EncounterOutcome(BaseModel):
    """A decided encounter. `winner_team` (or None for a draw) + `out_of_fight` (actors removed
    — downed/dead/fled, the ruleset decides what that means) + `survivors`. No 'casualties' as a
    death claim: whether out-of-fight means dead is the ruleset's call and the caller's (a lost
    brawl → wounded, not a corpse)."""

    winner_team: str | None = None
    survivors: list[str] = Field(default_factory=list)
    out_of_fight: list[str] = Field(default_factory=list)
    detail: dict[str, Any] = Field(default_factory=dict)


class Ruleset(Protocol):
    """The mechanics plugin (docs/06). Deterministic: every random draw comes from `rng`. The
    core ring depends only on this Protocol; concrete rulesets are wired at the composition root
    and bound per world/campaign (docs/06, the registry — inc 6.2)."""

    id: str
    version: str

    def sheet_schema(self) -> dict[str, Any]: ...

    def new_character(self, spec: CharSpec, rng: Rng) -> Sheet: ...

    def progression(self, sheet: Sheet, award: Award) -> Sheet: ...

    def affordances(self) -> list[Affordance]: ...

    def resolve_check(self, req: CheckRequest, rng: Rng) -> CheckResult: ...

    # --- encounter mode (the runner drives the loop through these; the state is opaque) ---

    def start_encounter(self, ctx: EncounterCtx, rng: Rng) -> EncounterState: ...

    def current_actor(self, state: EncounterState) -> str | None:
        """The actor to act next, or None when the encounter is decided — this is how the runner
        walks turns without knowing the ruleset's turn structure (initiative vs move-exchange)."""
        ...

    def legal_actions(self, state: EncounterState, actor_id: str) -> list[ActionSpec]: ...

    def npc_action(self, state: EncounterState, actor_id: str, rng: Rng) -> Action: ...

    def resolve_action(
        self, state: EncounterState, action: Action, rng: Rng
    ) -> tuple[EncounterState, list[Effect]]: ...

    def is_over(self, state: EncounterState) -> EncounterOutcome | None: ...

    def sheets(self, state: EncounterState) -> dict[str, Sheet]:
        """The current opaque sheet of every combatant, by actor_id — the runner persists these
        as `SheetUpdated` at encounter end, so ruleset-shaped harm (hp, a harm clock, conditions)
        reaches projections without the pipeline knowing the sheet's shape."""
        ...
