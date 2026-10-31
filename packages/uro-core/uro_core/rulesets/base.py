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

A ruleset is constructed with its world's settings (`[ruleset.config]`, D-62) and validates them
itself; `config_schema()` publishes what it accepts. The port never reads them.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from uro_core.rulesets.rng import Rng

# The ruleset port's version (D-65). A ruleset declares the version it was written against as
# `api_version`; the registry refuses one that doesn't match, so a plugin built for an older or
# newer port fails loudly at load instead of breaking mid-beat. Bump it when the port changes.
# v1 = the D-63 port: settings (`config_schema`), `conclude_encounter`, `downtime`.
RULESET_API_VERSION = 1

# Every method a ruleset must provide (the Protocol below), checked by `ruleset_contract_problem`.
_PORT_METHODS = (
    "sheet_schema",
    "config_schema",
    "new_character",
    "progression",
    "affordances",
    "resolve_check",
    "start_encounter",
    "current_actor",
    "legal_actions",
    "npc_action",
    "resolve_action",
    "is_over",
    "sheets",
    "conclude_encounter",
    "downtime",
)


def ruleset_contract_problem(ruleset: Any, expected_id: str | None = None) -> str | None:
    """Why `ruleset` (an instance, or a ruleset class) can't serve on this port, or None: a str
    `id` (equal to `expected_id`, when given) and `version`, `api_version == RULESET_API_VERSION`,
    and every port method. Enforced by the registry on plugins and every built ruleset, and by the
    Engine on the ruleset it's given (D-65 review) — an incompatible one fails up front, never
    mid-beat."""
    rid = getattr(ruleset, "id", None)
    if not isinstance(rid, str) or not rid:
        return f"its id is {rid!r}, not a non-empty string"
    if expected_id is not None and rid != expected_id:
        return f"its id is {rid!r}, not its entry-point name {expected_id!r}"
    if not isinstance(getattr(ruleset, "version", None), str):
        return f"its version is {getattr(ruleset, 'version', None)!r}, not a string"
    declared = getattr(ruleset, "api_version", None)
    if declared != RULESET_API_VERSION:
        return f"built for ruleset port v{declared}; this engine implements v{RULESET_API_VERSION}"
    missing = [m for m in _PORT_METHODS if not callable(getattr(ruleset, m, None))]
    if missing:
        return f"missing port methods: {', '.join(missing)}"
    return None


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
    pipeline reads `trace` (→ the narrator) and `sheet`; `detail` carries ruleset specifics (the
    roll, the DC, the 2d6 sum…) for debugging without shaping the contract.

    `sheet` is the actor's sheet AFTER the check when the check itself changed it — the ruleset
    applies its own rules (a pushed pool's strain, XP marked on a miss), exactly as an encounter's
    final sheets carry its harm. The engine commits it as `SheetUpdated`, opaque. None = the check
    left the sheet as it was."""

    outcome: str
    trace: str
    detail: dict[str, Any] = Field(default_factory=dict)
    sheet: Sheet | None = None


# --- encounter mode ---


class Combatant(BaseModel):
    """A participant handed to `start_encounter` — actor id, team, and the opaque sheet. No
    initiative/hp here: how (or whether) turns are ordered is ruleset-internal state."""

    actor_id: str
    team: str  # e.g. "party" | "foes"; the ruleset's is_over decides what a decided fight means
    sheet: Sheet
    is_pc: bool = False  # a player character (engine-known): rulesets advance PCs, not every foe


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
    api_version: int  # the port version it implements (RULESET_API_VERSION)

    def sheet_schema(self) -> dict[str, Any]: ...

    def config_schema(self) -> dict[str, Any]:
        """JSON schema of the settings a world may give this ruleset (`[ruleset.config]`, D-62).
        The ruleset is constructed with them and must refuse an unknown or invalid key."""
        ...

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

    def conclude_encounter(
        self, state: EncounterState, outcome: EncounterOutcome, pcs: frozenset[str]
    ) -> dict[str, Sheet]:
        """Every combatant's sheet once the fight is over — what gets committed (D-63). The
        ruleset's own aftermath goes here: advancement for the player characters in `pcs` (a d20
        level for winning, a milestone for surviving, XP for a missed move), as its settings say.
        Called once, after `is_over`. Returning `sheets(state)` unchanged is a valid answer."""
        ...

    def downtime(self, sheet: Sheet, days: int) -> Sheet | None:
        """This sheet after `days` of in-fiction downtime — rest, recovery (D-63) — or None when
        downtime changes nothing. Called for each of the ruleset's sheets on a branch whose time
        skips; the engine never decides what rest means."""
        ...

    def sheets(self, state: EncounterState) -> dict[str, Sheet]:
        """The current opaque sheet of every combatant, by actor_id — the runner persists these
        as `SheetUpdated` at encounter end, so ruleset-shaped harm (hp, a harm clock, conditions)
        reaches projections without the pipeline knowing the sheet's shape."""
        ...
