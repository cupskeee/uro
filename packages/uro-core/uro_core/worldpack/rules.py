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

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from uro_core.domain import events as _events
from uro_core.domain.events import ThreadState

# The current grammar version. v2 adds the Computation Layer's counter conditions/actions (docs/19,
# D-34). The additions are purely additive, so v1 packs (no counters) remain valid — the engine
# accepts the whole SUPPORTED set; a pack outside it fails loud at parse. A counter-using pack
# should declare 2 so an old (v1) engine rejects it with a clear version error.
RULES_API_VERSION = 3
_SUPPORTED_VERSIONS = frozenset({1, 2, 3})  # v3 (D-40) adds multi-ref scopes; v1/v2 stay valid


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

# Relations a module MAY touch — non-authoritative social/political edges only. Never `owns`/`rules`
# (ownership/authority is canon, not a module's to assert). The gauntlet re-checks this (INC-3).
ModuleRel = Literal["knows", "at_war_with", "allied_with"]
CmpOp = Literal["==", "!=", ">=", "<=", ">", "<"]

_STRICT = ConfigDict(extra="forbid")  # an unknown key is an author error, not silently ignored


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


Action = Annotated[
    ActSetThreadState
    | ActCreateThread
    | ActRecordRumor
    | ActSpreadBelief
    | ActAddEdge
    | ActRemoveEdge
    | ActSetCounter
    | ActAdjustCounter
    | ActResetCounter,
    Field(discriminator="do"),
]


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
