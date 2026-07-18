"""Reaction-Layer rule packs (docs/17, D-33) — the CLOSED, declarative grammar a pack ships.

A pack authors DATA (`rules.yaml` / `agendas.yaml`), never code. This module is the schema: a
condition tree and an ACTION tagged-union, both closed Pydantic unions. Two structural properties
fall out for free (no interpreter or sandbox needed to get them):

- The grammar is total and pure — no loops, assignment, user functions, recursion, or float
  arithmetic. Every predicate maps to one deterministic projection read (INC-3 evaluates it).
- The ACTION union is physically INCAPABLE of naming a mechanical/lethal/canon event
  (`ActorDied`/`SheetUpdated`/`ItemTransferred`/`ActorCreated`/a `truth=true` claim). Mechanics
  stay the ruleset's; canon stays the extractor's. This union IS the trust fence (mirrors how the
  extractor's `Extraction` schema can only shape actors/claims).

There is no `eval`/parser here — YAML → validated Pydantic. The interpreter (`engines/rules.py`)
and the gauntlet (`engines/rules_gauntlet.py`) come in INC-3.
"""

from __future__ import annotations

import types
from typing import Annotated, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from uro_core.domain import events as _events
from uro_core.domain.events import ThreadState

# The current grammar version. v2 adds the Computation Layer's counter conditions/actions (docs/19,
# D-34). The additions are purely additive, so v1 packs (no counters) remain valid — the engine
# accepts the whole SUPPORTED set; a pack outside it fails loud at parse. A counter-using pack
# should declare 2 so an old (v1) engine rejects it with a clear version error.
RULES_API_VERSION = 5
# v5 (RL-6): $trigger.<field>-aware `when` + trigger.per_event. v4 (for_each/roll_table/
# expire_claims), v3 multi-ref, v2 counters all still valid (a $trigger/per_event pack declares 5).
_SUPPORTED_VERSIONS = frozenset({1, 2, 3, 4, 5})


def _event_payload_fields() -> dict[str, frozenset[str]]:
    """event_type → its payload's field names, derived from the domain payload models
    (`<Name>Payload` → event `<Name>`). Used to reject a rule trigger that would VALIDATE but
    never fire — an unknown event type (a typo like `CheckResolved`) or a `where` key that is not a
    real payload field (`actor.member_of`). Gap-report finding, hit independently by 3 games:
    accepted-but-inert is a sharper footgun than a loud refusal."""
    out: dict[str, frozenset[str]] = {}
    for name, obj in vars(_events).items():
        if name.endswith("Payload") and isinstance(obj, type) and issubclass(obj, BaseModel):
            out[name[: -len("Payload")]] = frozenset(obj.model_fields)
    out.setdefault("EdgeUpdated", out.get("EdgeAdded", frozenset()))  # reuses EdgeAddedPayload
    # CounterChanged is emitted ONLY by the reaction gauntlet, so it appears only in module beats —
    # which react() does not re-process (single-hop, no cascade until C6). A trigger on it would be
    # accepted-but-inert, so it is NOT triggerable yet (remove this line when C6 lands cascade).
    out.pop("CounterChanged", None)
    return out


_EVENT_FIELDS = _event_payload_fields()


def _is_str_scalar(ann: object) -> bool:
    """True if a payload field's type resolves to a single string value (str, str|None, a Literal of
    strings, or Optional thereof) — the only kind a `$trigger.<field>` ref can meaningfully bind. A
    list/int/dict field would `str()`-ify into something that never matches an entity ref."""
    if ann is str:
        return True
    origin = get_origin(ann)
    if origin is Literal:
        return all(isinstance(a, str) for a in get_args(ann))
    if origin is Union or origin is types.UnionType:  # str | None / Optional[str]
        non_none = [a for a in get_args(ann) if a is not type(None)]
        return len(non_none) == 1 and _is_str_scalar(non_none[0])
    return False  # list[...]/int/dict/bool → not a bindable ref


def _event_str_fields() -> dict[str, frozenset[str]]:
    """Per event, the payload fields a `$trigger.<field>` ref MAY bind — the string-scalar ones. A
    ref on a non-string field (e.g. `ClaimRecorded.subject_refs`, a list) validates its NAME but
    would silently never match; RL-6 rejects it at parse (the anti-accepted-but-inert rule)."""
    out: dict[str, frozenset[str]] = {}
    for name, obj in vars(_events).items():
        if name.endswith("Payload") and isinstance(obj, type) and issubclass(obj, BaseModel):
            out[name[: -len("Payload")]] = frozenset(
                f for f, info in obj.model_fields.items() if _is_str_scalar(info.annotation)
            )
    out.setdefault("EdgeUpdated", out.get("EdgeAdded", frozenset()))
    return out


_EVENT_STR_FIELDS = _event_str_fields()

# Relations a module MAY touch — non-authoritative social/political edges only. Never `owns`/`rules`
# (ownership/authority is canon, not a module's to assert). The gauntlet re-checks this (INC-3).
ModuleRel = Literal["knows", "at_war_with", "allied_with"]
CmpOp = Literal["==", "!=", ">=", "<=", ">", "<"]

_STRICT = ConfigDict(extra="forbid")  # an unknown key is an author error, not silently ignored
_MAX_NESTED = 16  # C3/C4 (D-34 review): cap a for_each/roll_table's nested action list at PARSE, so
# recursion cost is bounded before the gauntlet ever runs (an unbounded list is a CPU/OOM DoS).


# --- Conditions: a closed, total predicate tree over the projection view + the trigger events ---


class CondThreadState(BaseModel):
    model_config = _STRICT
    kind: Literal["thread_state"]
    thread: str
    state: ThreadState


class CondActorTier(BaseModel):
    model_config = _STRICT
    kind: Literal["actor_tier"]
    actor: str
    op: CmpOp
    value: int = Field(ge=0, le=3)


class CondActorIsPC(BaseModel):
    model_config = _STRICT
    kind: Literal["actor_is_pc"]
    actor: str


class CondEdgeExists(BaseModel):
    model_config = _STRICT
    kind: Literal["edge_exists"]
    src: str
    rel: str
    dst: str


class CondWorldDay(BaseModel):
    model_config = _STRICT
    kind: Literal["world_day"]
    op: CmpOp
    value: int = Field(ge=0)


class CondCounter(BaseModel):
    """Compare a Computation-Layer counter to a constant (docs/19, D-34): the threshold read that
    RL-1 (tension→war, heat→lockdown, dread-after-3) needs. Single-entity in C1."""

    model_config = _STRICT
    kind: Literal["counter"]
    scope_ref: str
    key: str
    op: CmpOp
    value: int


class CounterRef(BaseModel):
    model_config = _STRICT
    scope_ref: str
    key: str


class CondCounterCompare(BaseModel):
    """Cross-entity integer counter comparison (docs/19 C2, RL-3): evaluates
    `left * left_mul  OP  right * right_mul` — integer cross-multiply, so `strength(A) >
    strength(B)*1.2` is `left_mul=5, right_mul=6, op=">"` with no float. A READ (conditions read
    freely, like actor_tier); the cross-entity ACTIONS it gates need a `world` scope."""

    model_config = _STRICT
    kind: Literal["counter_compare"]
    left: CounterRef
    right: CounterRef
    op: CmpOp
    left_mul: int = 1
    right_mul: int = 1


class CondCountEdges(BaseModel):
    """Count a relation out of `src` and compare (docs/19 C2, RL-5 fall-of-house: owns == 0).
    Reuses `edges_from`; no per-member counters needed (the critic's lighter mechanism)."""

    model_config = _STRICT
    kind: Literal["count_edges"]
    src: str
    rel: str
    op: CmpOp
    value: int = Field(ge=0)


class CondAll(BaseModel):
    model_config = _STRICT
    kind: Literal["all"]
    all: list[Condition] = Field(min_length=1)


class CondAny(BaseModel):
    model_config = _STRICT
    kind: Literal["any"]
    any: list[Condition] = Field(min_length=1)


class CondNot(BaseModel):
    model_config = _STRICT
    kind: Literal["not"]
    cond: Condition


Condition = Annotated[
    CondThreadState
    | CondActorTier
    | CondActorIsPC
    | CondEdgeExists
    | CondWorldDay
    | CondCounter
    | CondCounterCompare
    | CondCountEdges
    | CondAll
    | CondAny
    | CondNot,
    Field(discriminator="kind"),
]


# RL-6: which slots of each leaf condition are ENTITY REFS (may hold a `$trigger.<field>` bound from
# the trigger payload) vs literal values. A `$trigger` in a literal slot (op/value/key/rel/state) is
# a PARSE error, not a silent type-confusion misfire (e.g. an actor id resolved into a counter key).
# counter_compare's refs are nested in left/right and are handled explicitly by the walker.
_COND_REF_SLOTS: dict[str, frozenset[str]] = {
    "thread_state": frozenset({"thread"}),
    "actor_tier": frozenset({"actor"}),
    "actor_is_pc": frozenset({"actor"}),
    "edge_exists": frozenset({"src", "dst"}),
    "world_day": frozenset(),
    "counter": frozenset({"scope_ref"}),
    "counter_compare": frozenset(),  # nested: left.scope_ref / right.scope_ref
    "count_edges": frozenset({"src"}),
}
_TRIGGER_PREFIX = "$trigger."


def _check_trigger_refs(cond: Condition, event: str | None, rule_id: str) -> None:
    """Validate every `$trigger.<field>` in a condition tree (RL-6). A `$trigger` ref is legal ONLY
    in an entity-ref slot (per `_COND_REF_SLOTS`) of a RULE (an event to bind from), and its
    `<field>` must be a real payload field of that event (reusing `_EVENT_FIELDS`, the same catalog
    that fences `trigger.where`). In a literal slot, or in an AGENDA rule (`event is None`, no
    trigger to bind), it is a loud parse error — closing the accepted-but-inert + type-confusion
    footguns the way `_trigger_can_fire` already closed them for `trigger.where`."""
    kind = cond.kind
    if kind == "all":
        for c in cond.all:  # type: ignore[union-attr]
            _check_trigger_refs(c, event, rule_id)
        return
    if kind == "any":
        for c in cond.any:  # type: ignore[union-attr]
            _check_trigger_refs(c, event, rule_id)
        return
    if kind == "not":
        _check_trigger_refs(cond.cond, event, rule_id)  # type: ignore[union-attr]
        return
    ref_slots = _COND_REF_SLOTS.get(kind, frozenset())
    if kind == "counter_compare":
        slots = [
            ("left.scope_ref", cond.left.scope_ref, True),  # type: ignore[union-attr]
            ("left.key", cond.left.key, False),  # type: ignore[union-attr]
            ("right.scope_ref", cond.right.scope_ref, True),  # type: ignore[union-attr]
            ("right.key", cond.right.key, False),  # type: ignore[union-attr]
        ]
    else:
        slots = [
            (name, getattr(cond, name), name in ref_slots)
            for name in type(cond).model_fields
            if isinstance(getattr(cond, name), str)
        ]
    legal = ", ".join(sorted(n for n, _, isref in slots if isref)) or "none"
    for name, value, is_ref in slots:
        if not value.startswith(_TRIGGER_PREFIX):
            continue
        field_name = value[len(_TRIGGER_PREFIX) :]
        if event is None:
            raise ValueError(
                f"rule {rule_id!r}: {value!r} in an agenda-rule condition, which has no trigger "
                f"event to bind $trigger from"
            )
        if not is_ref:
            raise ValueError(
                f"rule {rule_id!r}: {value!r} in the literal slot {name!r} of a {kind!r} condition "
                f"- $trigger is only bindable in an entity-ref slot ({legal})"
            )
        if field_name not in _EVENT_FIELDS.get(event, frozenset()):
            fields = ", ".join(sorted(_EVENT_FIELDS.get(event, frozenset())))
            raise ValueError(
                f"rule {rule_id!r}: {value!r} is not a field of {event} (fields: {fields}) - it "
                f"would validate but never bind"
            )
        if field_name not in _EVENT_STR_FIELDS.get(event, frozenset()):
            raise ValueError(
                f"rule {rule_id!r}: {value!r} is not a string ref field of {event} (it is "
                f"list/int-typed) - $trigger binds one string ref, so it could never match"
            )


# --- Actions: the CLOSED emit union (the structural fence) ---


class ActSetThreadState(BaseModel):
    model_config = _STRICT
    do: Literal["set_thread_state"]
    thread: str
    to: ThreadState


# Computation Layer (docs/19, D-34): bounded integer-counter writes → CounterChanged events. The
# gauntlet clamps to ±_MAX_COUNTER, scope-fences scope_ref, and accumulates within a pass
# (read-your-writes). `adjust`'s delta is a LITERAL — computed-from-other-counter deltas (economy
# formulas, RL-2/RL-6) are deliberately NOT in this tier (docs/19 OQ-1).
class ActSetCounter(BaseModel):
    model_config = _STRICT
    do: Literal["set_counter"]
    scope_ref: str
    key: str
    value: int


class ActAdjustCounter(BaseModel):
    model_config = _STRICT
    do: Literal["adjust_counter"]
    scope_ref: str
    key: str
    delta: int


class ActResetCounter(BaseModel):
    model_config = _STRICT
    do: Literal["reset_counter"]
    scope_ref: str
    key: str


class ActCreateThread(BaseModel):
    model_config = _STRICT
    do: Literal["create_thread"]
    thread: str
    stakes: str  # always minted dormant, provenance=module (never author canon)


class ActRecordRumor(BaseModel):
    model_config = _STRICT
    do: Literal["record_rumor"]
    text: str  # → a ClaimRecorded FORCED truth=unknown, origin=module (testimony, never canon)
    subjects: list[str] = Field(default_factory=list)


class ActSpreadBelief(BaseModel):
    model_config = _STRICT
    do: Literal["spread_belief"]
    claim: str
    # → a capped propagate_belief fan-out, caused_by=module
    witnesses: list[str] = Field(min_length=1)


class ActAddEdge(BaseModel):
    model_config = _STRICT
    do: Literal["add_edge"]
    src: str
    rel: ModuleRel
    dst: str


class ActRemoveEdge(BaseModel):
    model_config = _STRICT
    do: Literal["remove_edge"]
    src: str
    rel: ModuleRel
    dst: str


class ActExpireClaims(BaseModel):
    """C5 (D-34, RL-8): retract stale MODULE rumors. STRUCTURALLY only ever affects `origin=module`,
    non-canon (`truth != true`) claims IN SCOPE older than `older_than_days` — a module rule can
    never retract narrator canon. Emits `ClaimTruthChanged(truth=false)`, bounded + baked ids. (No
    `where` filter: module-only is already structural, so any filter would be redundant or a
    silently-inert footgun — D-34 review.)"""

    model_config = _STRICT
    do: Literal["expire_claims"]
    older_than_days: int = Field(ge=1)


class ActRollTable(BaseModel):
    """C4 (D-34, RL-4 / Seventh RL-6): a seeded, deterministic WEIGHTED choice over named outcomes.
    Picks one outcome via an integer hash of (trigger commit, rule, action index) weighted by
    `weights`, then applies that outcome's nested LEAF actions (node-budget capped). The pick is
    BAKED into events (replay re-applies the same events, never re-rolls)."""

    model_config = _STRICT
    do: Literal["roll_table"]
    weights: dict[str, int]  # outcome name → positive integer weight
    outcomes: dict[str, list[Action]]  # outcome name → nested actions

    @model_validator(mode="after")
    def _weights_and_leaf(self) -> ActRollTable:
        if not self.weights:  # an empty table has no outcome to pick — a ZeroDivision at runtime
            raise ValueError("roll_table: at least one weighted outcome is required")
        if set(self.weights) != set(self.outcomes):
            raise ValueError("roll_table: `weights` and `outcomes` must name the same outcomes")
        if any(w <= 0 for w in self.weights.values()):
            raise ValueError("roll_table: weights must be positive integers")
        if len(self.outcomes) > _MAX_NESTED or any(
            len(acts) > _MAX_NESTED for acts in self.outcomes.values()
        ):
            raise ValueError(f"roll_table: at most {_MAX_NESTED} outcomes, each that many acts")
        _reject_nested(a for acts in self.outcomes.values() for a in acts)
        return self


class ActForEach(BaseModel):
    """C3 (D-34, RL-11): ONE bounded loop over a ref's edge-neighbors (single-hop). Traverse
    `traverse` edges from `from` (a literal ref or `$trigger.<field>`), bind each neighbor as `as`,
    and apply the nested `apply` LEAF actions with the bind var + `$trigger.<field>` substituted —
    each neighbor scope-fenced, fan-out + node-budget capped. (The wished `do:[...]` inner key is
    `apply` here: `do` is the action discriminator.)"""

    model_config = _STRICT
    do: Literal["for_each"]
    traverse: ModuleRel
    source: str = Field(alias="from")  # a literal ref, or `$trigger.<field>`
    bind: str = Field(alias="as")  # the loop-variable token substituted into `apply`
    apply: list[Action] = Field(min_length=1, max_length=_MAX_NESTED)  # bounded (DoS, D-34 review)

    @model_validator(mode="after")
    def _leaf_only(self) -> ActForEach:
        _reject_nested(self.apply)
        return self


def _reject_nested(actions) -> None:  # type: ignore[no-untyped-def]
    """A recursive action (for_each/roll_table) may nest only LEAF actions — no loop-in-a-loop:
    keeps 'the ONE bounded loop' true and cost O(fan-out), not O(fan-out^n) (D-34)."""
    nested = [a.do for a in actions if a.do in ("for_each", "roll_table")]
    if nested:
        raise ValueError(f"for_each/roll_table may not nest another loop/table (got {nested})")


Action = Annotated[
    ActSetThreadState
    | ActCreateThread
    | ActRecordRumor
    | ActSpreadBelief
    | ActAddEdge
    | ActRemoveEdge
    | ActSetCounter
    | ActAdjustCounter
    | ActResetCounter
    | ActExpireClaims
    | ActRollTable
    | ActForEach,
    Field(discriminator="do"),
]

# Resolve the forward references: roll_table/for_each nest `Action`, defined just above.
ActRollTable.model_rebuild()
ActForEach.model_rebuild()


# --- Scope (jurisdiction) + Rule + RulePack ---


class Scope(BaseModel):
    """A rule's jurisdiction — the gauntlet drops any emitted ref outside it (the generalization of
    D-32 participant-scoping). Set `world: true` for a whole-realm rule, else EXACTLY ONE category:
    thread(s) / faction(s) / place(s). `world` (docs/19 C2, OQ-2) is the first-class form of the
    umbrella-faction hack Sable/Ironwake used for cross-entity rules — it relaxes the JURISDICTION
    fence (any ref reachable), NOT the action fence: a rule can still only emit the closed,
    non-canon Action union (no mint/kill/loot/truth=true), so a realm-wide rule may adjust any
    counter or move a non-authoritative edge, never assert canon.

    MULTI-REF (D-40, B11): the plural forms (`factions: [a, b]`) let a rule span several entities of
    ONE category — a pact BETWEEN two factions, a thread across three — without the blunt `world`
    scope (least-privilege middle ground). The singular and plural of one category merge; mixing
    categories, or a category with `world`, is rejected (a scope names one jurisdiction)."""

    model_config = _STRICT
    world: bool = False  # whole-realm jurisdiction (cross-entity rules); takes precedence
    thread: str | None = None  # the thread's stakeholders
    faction: str | None = None  # a faction's members
    place: str | None = None  # a place's occupants
    threads: list[str] = Field(default_factory=list)  # multi-ref (D-40): several threads
    factions: list[str] = Field(default_factory=list)  # e.g. a pact BETWEEN factions
    places: list[str] = Field(default_factory=list)  # several places' occupants

    def refs(self) -> dict[str, list[str]]:
        """The merged jurisdiction per category (singular folded into plural), for the gauntlet."""
        return {
            "thread": ([self.thread] if self.thread else []) + self.threads,
            "faction": ([self.faction] if self.faction else []) + self.factions,
            "place": ([self.place] if self.place else []) + self.places,
        }

    @model_validator(mode="after")
    def _one_jurisdiction(self) -> Scope:
        set_cats = [name for name, refs in self.refs().items() if refs]
        if self.world and set_cats:
            raise ValueError("scope: `world` is exclusive — do not also set thread/faction/place")
        if len(set_cats) > 1:
            raise ValueError(f"scope: name exactly ONE jurisdiction category, got {set_cats}")
        if not self.world and not set_cats:
            raise ValueError(
                "scope: set `world: true` or one of thread(s)/faction(s)/place(s) — an empty scope "
                "would drop every action"
            )
        return self


class Trigger(BaseModel):
    model_config = _STRICT
    event: str  # an event_type that must appear in the triggering beat (e.g. "ActorDied")
    where: dict[str, str] = Field(default_factory=dict)  # optional payload field==value matches
    # RL-6: `when` is evaluated per matching trigger event. Default (False) fires the rule ONCE,
    # bound to the first event satisfying trigger∧when (the existential "ANY member died"). True
    # fires it once PER matching event (the count-each shape, e.g. adjust_counter per dead member) -
    # each event's emissions get distinct, idempotent ids (see FiredAction.event_key).
    per_event: bool = False


class Rule(BaseModel):
    model_config = _STRICT
    id: str
    trigger: Trigger  # NB: not `on` — YAML 1.1 parses a bare `on:` key as the boolean True
    when: Condition | None = None
    then: list[Action] = Field(min_length=1)
    scope: Scope

    @model_validator(mode="after")
    def _trigger_can_fire(self) -> Rule:
        # Reject a trigger that would VALIDATE but never fire (gap-report footgun): an unknown event
        # type, or a `where` key that is not a real field of that event's payload. Caught at parse
        # (and, via create_world, at world creation) instead of silently never matching at runtime.
        ev = self.trigger.event
        if ev not in _EVENT_FIELDS:
            raise ValueError(
                f"rule {self.id!r}: trigger.event {ev!r} is not a known event type — it would "
                f"validate but never fire. Known event types: {', '.join(sorted(_EVENT_FIELDS))}"
            )
        unknown = [k for k in self.trigger.where if k not in _EVENT_FIELDS[ev]]
        if unknown:
            raise ValueError(
                f"rule {self.id!r}: trigger.where key(s) {unknown} are not fields of {ev} "
                f"(fields: {', '.join(sorted(_EVENT_FIELDS[ev]))}) — the filter could never match"
            )
        if self.when is not None:  # RL-6: fence $trigger.<field> refs in the `when` tree
            _check_trigger_refs(self.when, ev, self.id)
        return self


class AgendaRule(BaseModel):
    """A downtime/agenda rule (INC-4): fires at the time-skip boundary keyed off in-fiction day,
    not off a triggering event. Same condition/action grammar, no `on` event trigger."""

    model_config = _STRICT
    id: str
    every_days: int = Field(ge=1)  # cadence in in-fiction days
    when: Condition | None = None
    then: list[Action] = Field(min_length=1)
    scope: Scope

    @model_validator(mode="after")
    def _no_trigger_refs(self) -> AgendaRule:
        # An agenda rule has no trigger event, so a $trigger.<field> ref could never bind - reject
        # it loudly at parse (RL-6) rather than fail-closed-and-silent at runtime.
        if self.when is not None:
            _check_trigger_refs(self.when, None, self.id)
        return self


class RulePack(BaseModel):
    model_config = _STRICT
    rules_api_version: int
    rules: list[Rule] = Field(default_factory=list)
    agendas: list[AgendaRule] = Field(default_factory=list)

    @field_validator("rules_api_version")
    @classmethod
    def _pin_version(cls, v: int) -> int:
        # The version pin is enforced on the MODEL, not just at parse — so it holds on every
        # RulePack construction: parse_pack, the runtime _react/agenda_tick path (rebuilt from the
        # inline WorldGenesis payload), and import (which replays that payload without re-parsing).
        # Closes the P5 seam where an imported pack would run under the wrong semantics (docs/17).
        if v not in _SUPPORTED_VERSIONS:
            raise ValueError(
                f"rules_api_version {v} unsupported; this engine supports "
                f"{sorted(_SUPPORTED_VERSIONS)}"
            )
        return v


# resolve the recursive Condition forward refs
CondAll.model_rebuild()
CondAny.model_rebuild()
CondNot.model_rebuild()
