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

from pydantic import BaseModel, ConfigDict, Field, field_validator

from uro_core.domain.events import ThreadState

# The one supported grammar version. A pack MUST pin `rules_api_version`; a mismatch fails loud at
# parse (closes the reserved-but-unenforced TEMPLATE_API_VERSION gap — decided-OQ, docs/17).
RULES_API_VERSION = 1

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
    | ActRemoveEdge,
    Field(discriminator="do"),
]


# --- Scope (jurisdiction) + Rule + RulePack ---


class Scope(BaseModel):
    """A rule's jurisdiction — the gauntlet (INC-3) drops any emitted ref outside it (the
    generalization of D-32 participant-scoping). Exactly one of the fields is set."""

    model_config = _STRICT
    thread: str | None = None  # the thread's stakeholders
    faction: str | None = None  # a faction's members
    place: str | None = None  # a place's occupants


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
        # Closes the P5 seam where an imported v2 pack would run under v1 semantics (docs/17, D-33).
        if v != RULES_API_VERSION:
            raise ValueError(
                f"rules_api_version {v} unsupported; this engine supports {RULES_API_VERSION}"
            )
        return v


# resolve the recursive Condition forward refs
CondAll.model_rebuild()
CondAny.model_rebuild()
CondNot.model_rebuild()
