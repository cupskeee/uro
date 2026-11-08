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

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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


# How dangerous an actor is, in words every game can read (D-112): the one engine-defined hint a
# spec carries. A pack's actor or the extractor may set it; the RULESET decides what it means (a
# level, a dice pool, nothing at all). "" = no hint.
THREAT_LEVELS: tuple[str, ...] = ("harmless", "weak", "average", "strong", "deadly")


class CharSpec(BaseModel):
    """Ruleset-opaque character-creation input. The ruleset interprets `data` (uro_basic reads
    `abilities`/`level`/`weapon_tier`; uro_pbta reads its stat spread + playbook). `threat` is the
    engine's one neutral hint (`THREAT_LEVELS`, D-112); a ruleset may read it or ignore it, and a
    value in `data` should win over what the hint implies."""

    data: dict[str, Any] = Field(default_factory=dict)
    threat: str = ""


class SpecProblem(BaseModel):
    """One reason a character spec can't be built (D-112), located like a rule-pack problem:
    `loc` is the path inside the spec's `data` (empty = the spec as a whole)."""

    loc: list[str | int] = Field(default_factory=list)
    message: str


class CharSpecError(ValueError):
    """`new_character` refuses a spec (D-112) with the ruleset's own problems. A ruleset may raise
    this, or a pydantic `ValidationError` from its own spec model; `build_character` turns either
    into problems. A ruleset that never raises accepts every spec, as before."""

    def __init__(self, problems: list[SpecProblem]) -> None:
        self.problems = problems
        super().__init__("; ".join(_problem_text(p) for p in problems) or "invalid spec")


def _problem_text(p: SpecProblem) -> str:
    return f"{'.'.join(str(x) for x in p.loc)}: {p.message}" if p.loc else p.message


def spec_problems(exc: Exception) -> list[SpecProblem]:
    """The located problems of a refused spec: a `CharSpecError`'s own, or a pydantic
    `ValidationError`'s (the ruleset validated `data` into its spec model)."""
    if isinstance(exc, CharSpecError):
        return exc.problems
    if isinstance(exc, ValidationError):
        return [
            SpecProblem(loc=[x for x in e["loc"] if isinstance(x, str | int)], message=e["msg"])
            for e in exc.errors(include_url=False)
        ]
    return [SpecProblem(message=str(exc) or type(exc).__name__)]


def build_character(ruleset: Any, spec: CharSpec, rng: Any) -> Sheet:
    """`ruleset.new_character`, with a refused spec as a `CharSpecError` whatever the ruleset
    raised (its own error, or a `ValidationError`/`ValueError`/`TypeError` from validating), and
    an unknown threat word refused before the ruleset sees it."""
    if spec.threat and spec.threat not in THREAT_LEVELS:
        raise CharSpecError(
            [SpecProblem(loc=["threat"], message=f"one of {', '.join(THREAT_LEVELS)}")]
        )
    try:
        sheet: Sheet = ruleset.new_character(spec, rng)
    except CharSpecError:
        raise
    except (ValidationError, ValueError, TypeError, KeyError) as exc:
        raise CharSpecError(spec_problems(exc)) from exc
    return sheet


def spec_schema_of(ruleset: Any) -> dict[str, Any]:
    """The JSON Schema of the `data` a ruleset's `new_character` takes (D-112), or `{}` (any object)
    for a ruleset that doesn't declare one. Optional on the port, so a v1 plugin still loads."""
    fn = getattr(ruleset, "spec_schema", None)
    return dict(fn()) if callable(fn) else {}


def check_holdings(ruleset: Any, holdings: list[Holding]) -> None:
    """A ruleset's say on the things a new character starts holding (D-113): its optional
    `check_holdings(holdings)` hook may raise `CharSpecError` (or a `ValidationError`/`ValueError`,
    turned into problems). A ruleset without the hook accepts any holdings."""
    fn = getattr(ruleset, "check_holdings", None)
    if not callable(fn):
        return
    try:
        fn(holdings)
    except CharSpecError:
        raise
    except (ValidationError, ValueError, TypeError, KeyError) as exc:
        raise CharSpecError(spec_problems(exc)) from exc


class Choice(BaseModel):
    """A choice a ruleset leaves for a player about their character (D-113): how it grows, what a
    setback costs. Ruleset-shaped and opaque to the engine: an id, the words to show, the options
    (each whatever the ruleset needs; it reads the answer). It lives in the SHEET, so it is
    event-sourced, forks with the character, and play never waits on it."""

    id: str
    prompt: str
    options: list[dict[str, Any]] = Field(default_factory=list)


def pending_choices(ruleset: Any, sheet: Sheet | None) -> list[Choice]:
    """The choices waiting on a character (D-113): the ruleset's optional `pending_choices(sheet)`,
    or none for a ruleset without the hook (or no sheet)."""
    fn = getattr(ruleset, "pending_choices", None)
    if sheet is None or not callable(fn):
        return []
    return [c if isinstance(c, Choice) else Choice.model_validate(c) for c in fn(sheet)]


def answer_choice(ruleset: Any, sheet: Sheet, choice_id: str, answer: dict[str, Any]) -> Sheet:
    """The sheet after a player answers one of its pending choices (D-113), through the ruleset's
    optional `answer_choice(sheet, choice_id, answer)`. An answer it refuses raises
    `CharSpecError` (located under `answer`); a choice that isn't pending, too."""
    if choice_id not in {c.id for c in pending_choices(ruleset, sheet)}:
        raise CharSpecError([SpecProblem(loc=["choice_id"], message="no such pending choice")])
    fn = getattr(ruleset, "answer_choice", None)
    if not callable(fn):  # a choice it raised but can't take: the ruleset's own fault
        raise CharSpecError([SpecProblem(message="this ruleset can't take an answer")])
    try:
        new: Sheet = fn(sheet, choice_id, answer)
    except CharSpecError as exc:
        raise CharSpecError(
            [SpecProblem(loc=["answer", *p.loc], message=p.message) for p in exc.problems]
        ) from exc
    except (ValidationError, ValueError, TypeError, KeyError) as exc:
        raise CharSpecError(
            [SpecProblem(loc=["answer", *p.loc], message=p.message) for p in spec_problems(exc)]
        ) from exc
    return new


class Holding(BaseModel):
    """Something an actor holds (D-112), as checks and encounters see it: the item's id and name,
    and whatever opaque data a pack or a ruleset attached. Holding stays world state
    (`ItemCreated`/`ItemTransferred`); a ruleset may read this or ignore it."""

    item_id: str
    name: str
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
    holdings: list[Holding] = Field(default_factory=list)  # what the actor holds (D-112)


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
    # The part of this check a PLAYER may see (D-112): the ruleset's choice, e.g. the acting
    # character's own dice, modifier and the band reached, never an opponent's hidden values.
    # None = nothing beyond `outcome` (the D-46 default). Sent on `beat_checks`/`beat_committed`.
    player_view: dict[str, Any] | None = None


# --- encounter mode ---


class Combatant(BaseModel):
    """A participant handed to `start_encounter` — actor id, team, and the opaque sheet. No
    initiative/hp here: how (or whether) turns are ordered is ruleset-internal state."""

    actor_id: str
    team: str  # e.g. "party" | "foes"; the ruleset's is_over decides what a decided fight means
    sheet: Sheet
    is_pc: bool = False  # a player character (engine-known): rulesets advance PCs, not every foe
    holdings: list[Holding] = Field(default_factory=list)  # what it holds (D-112)


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

    # Optional since D-112 (a ruleset without it still loads): `spec_schema()`, the JSON Schema of
    # the `CharSpec.data` its `new_character` takes; read through `spec_schema_of`.

    def config_schema(self) -> dict[str, Any]:
        """JSON schema of the settings a world may give this ruleset (`[ruleset.config]`, D-62).
        The ruleset is constructed with them and must refuse an unknown or invalid key."""
        ...

    def new_character(self, spec: CharSpec, rng: Rng) -> Sheet:
        """A sheet from a spec. May refuse one it can't build (D-112): raise `CharSpecError`, or
        let a `ValidationError` from its own spec model escape; an EMPTY spec must always build
        (it is what an unsheeted actor gets). Callers go through `build_character`."""
        ...

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
