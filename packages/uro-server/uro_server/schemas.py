"""The HTTP API's response shapes, so `/openapi.json` describes what every endpoint returns and a
client can generate its types from it.

Each route declares one of these as its `response_model`, so FastAPI validates the handler's
output against it on every response. Every shape a handler builds by hand is strict (an undeclared
key fails, and a test that calls the route catches it): the envelopes below, the state rows, and
the core models a route returns whole (`WorldOut`, `MarkerOut`, …). A core model nested in an
envelope is a dump of that model itself (`ModelConnection`, `Campaign`, …), so it matches by
construction; it is not checked for extra keys. Routes are declared with
`response_model_exclude_unset`, so a key a handler leaves out stays out (no `null` appears that
wasn't sent before). Timestamps are ISO 8601 as pydantic writes them (`…Z` for UTC).

State rows mirror the projection catalog (`_SNAPSHOT_TABLES` in the Postgres projector); a test
keeps the two in lockstep. Ruleset sheets, edge attributes and event payloads are opaque by design
(D-30) and stay `dict` here.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from uro_core.chronicler import ReceiptEntry
from uro_core.domain.events import BeatResolvedPayload, DomainEvent
from uro_core.domain.extraction_policy import ExtractionPolicy
from uro_core.engines.probe import ProbeResult
from uro_core.export import BundleCommit, BundleEvent, WorldBundle
from uro_core.pipeline.pacing import PacingReport
from uro_core.ports.model_registry import ModelConnection, ProviderCredential, RoleBinding
from uro_core.ports.tokens import TokenInfo
from uro_core.timeline.models import (
    Branch,
    BranchInfo,
    Campaign,
    LineageEntry,
    Marker,
    ParticipantNote,
    World,
)
from uro_core.worldpack.create import SeedResult
from uro_core.worldpack.models import (
    ActorSeed,
    ClaimSeed,
    FactionSeed,
    PlaceSeed,
    ThreadSeed,
    WorldPack,
)
from uro_core.worldpack.sufficiency import SufficiencyDimension


class Out(BaseModel):
    """A response envelope: an undeclared key fails validation, so a handler and its schema
    can't drift apart."""

    model_config = ConfigDict(extra="forbid")


# --- core models a route returns whole, made strict: a handler that builds one by hand (POST
# /worlds does) can't add a key the model doesn't have. Nested core models are dumps of the model
# itself, so they match it by construction. ---


class WorldOut(World):
    model_config = ConfigDict(extra="forbid")


class MarkerOut(Marker):
    model_config = ConfigDict(extra="forbid")


class SeedResultOut(SeedResult):
    model_config = ConfigDict(extra="forbid")


class ExtractionPolicyOut(ExtractionPolicy):
    model_config = ConfigDict(extra="forbid")


class CommitOut(BundleCommit):
    model_config = ConfigDict(extra="forbid")


class WorldBundleOut(WorldBundle):
    model_config = ConfigDict(extra="forbid")


# --- small acknowledgements ---


class Status(Out):
    status: str


class Versions(Out):
    uro_server: str = Field(alias="uro-server")
    uro_core: str = Field(alias="uro-core")


class Created(Out):
    id: str


class Updated(Out):
    updated: bool


class Deleted(Out):
    deleted: bool


class Removed(Out):
    """A world, branch or campaign removal (D-99): rows removed, per kind."""

    removed: dict[str, int]


class Rotated(Out):
    rotated: bool


# --- metering (D-66) ---


class UsageRow(Out):
    stage_tag: str
    model: str | None
    calls: int
    tokens_in: int
    tokens_out: int
    avg_latency_ms: int | None
    failed: int


class Usage(Out):
    stage: str | None
    total_calls: int
    by_stage: list[UsageRow]


class LLMCallRow(Out):
    call_id: str
    stage_tag: str
    model: str | None
    tokens_in: int | None
    tokens_out: int | None
    latency_ms: int
    world_id: str | None
    branch_id: str | None
    created_at: str  # ISO 8601, as stored
    response: str | None
    error: str | None


class LLMCalls(Out):
    calls: list[LLMCallRow]


# --- rulesets (D-30, D-65) ---


class RulesetInfo(Out):
    id: str
    version: str | None = None
    sheet_schema: dict[str, Any] | None = None
    config_schema: dict[str, Any] | None = None
    error: str | None = None  # set instead of the schemas when it can't be listed


class Rulesets(Out):
    rulesets: list[RulesetInfo]
    unusable: dict[str, str]  # installed plugins that can't be used → why


# --- the model-connection registry (D-47) ---


class Providers(Out):
    connections: list[ModelConnection]
    roles: list[RoleBinding]
    credentials: list[ProviderCredential]


class RoleSet(Out):
    role: str
    connection_id: str
    reasoning_effort: str | None


class ModelInfo(Out):
    id: str
    modality: str  # chat | embedding | unknown


class Models(Out):
    models: list[ModelInfo]


class ProbeOutcome(Out):
    ok: bool
    detail: str


class Reloaded(Out):
    reloaded: bool
    detail: str | None = None


class CodexLogin(Out):
    login_id: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int


class CodexPoll(Out):
    status: str  # pending | connected
    connection_id: str | None = None
    credential_id: str | None = None
    models: list[ModelInfo] | None = None


# --- Chronicler (D-25, D-32) ---


class OutcomeRecorded(Out):
    committed_events: int
    commit_id: str
    receipt: list[ReceiptEntry]


# --- worlds and packs ---


class PackCounts(Out):
    places: int
    actors: int
    factions: int
    threads: int


class PackValidation(Out):
    name: str
    grade: str
    counts: PackCounts
    dimensions: list[SufficiencyDimension]
    ruleset_id: str
    ruleset_ok: bool
    ruleset_problem: str | None
    gaps: list[str]


class ParsedPack(WorldPack):
    """A pack as Uro reads it (D-105): `WorldPack`, every default filled in."""

    model_config = ConfigDict(extra="forbid")


class RulesSchema(Out):
    """The world-rules grammar (D-105): the version this engine writes, the versions it reads, and
    the JSON Schema of a rule pack."""

    rules_api_version: int
    supported_versions: list[int]
    rule_pack_schema: dict[str, Any]


class RuleProblem(Out):
    loc: list[str | int]  # where in the pack ([] for the pack as a whole)
    message: str


class RulesCheck(Out):
    """A rule pack checked on its own, as a world would check it (D-105)."""

    ok: bool
    rules: int
    agendas: int
    problems: list[RuleProblem]


class PackCreated(Out):
    world_id: str
    main_branch_id: str
    name: str
    grade: str
    backfilled: list[str]


class Backfill(Out):
    """A backfill preview (nothing committed). Each list holds only what the machine made, tagged
    `provenance=ai_backfill` (D-108); `filled` names the dimensions it filled."""

    name: str
    before_grade: str
    after_grade: str
    added: list[str]
    filled: list[str]
    seeds: list[ThreadSeed]  # conflict seeds
    places: list[PlaceSeed]
    factions: list[FactionSeed]
    actors: list[ActorSeed]
    claims: list[ClaimSeed]
    tone: list[str]


class Probe(Out):
    world: str
    results: list[ProbeResult]
    ok: bool  # no probe failed (warn-not-fail, D-24)
    warnings: list[str]


# --- timelines ---


class BranchRow(BranchInfo):
    model_config = ConfigDict(extra="forbid")
    world_day: int


class Branches(Out):
    branches: list[BranchRow]
    markers: list[Marker]


class Forked(Branch):
    model_config = ConfigDict(extra="forbid")
    world_day: int | None = None  # only after a time-skip on the new branch


class Log(Out):
    branch: str
    head_depth: int
    entries: list[LineageEntry]


class Events(Out):
    branch: str
    events: list[BundleEvent]


class Chronicle(Out):
    branch: str | None = None  # the world chronicle names its branch; a campaign's is its own
    beats: list[BeatResolvedPayload]


# --- projection state rows (one per `_SNAPSHOT_TABLES` section) ---


class ActorRow(Out):
    actor_id: str
    name: str
    tier: int
    role: str
    aliases: list[str]
    status: str
    provenance: str  # D-108: "author" | "ai_backfill"; "" = not recorded (see D-108's correction)


class ClaimRow(Out):
    claim_id: str
    statement: str
    subject_refs: list[str]
    truth: str
    origin: str
    # the in-fiction day a module claim was made (expire_claims ages it); 0 on every other origin
    # — display-only there, not a claim's age (D-85)
    created_day: int


class BeliefRow(Out):
    actor_id: str
    claim_id: str
    confidence: float
    learned_from: str | None


class PlaceRow(Out):
    place_id: str
    name: str
    kind: str
    status: str
    description: str
    provenance: str  # D-108


class PCRow(Out):
    campaign_id: str
    actor_id: str
    participant_id: str
    active: bool


class SheetRow(Out):
    actor_id: str
    ruleset_id: str
    sheet: dict[str, Any]  # the ruleset's own shape (D-30)


class ItemRow(Out):
    item_id: str
    name: str
    kind: str
    owner_ref: str


class FactionRow(Out):
    faction_id: str
    name: str
    kind: str
    description: str
    provenance: str  # D-108


class EdgeRow(Out):
    src: str
    rel_type: str
    dst: str
    weight: float
    attrs: dict[str, Any]


class ThreadRow(Out):
    thread_id: str
    stakes: str
    state: str
    provenance: str
    developments: int  # D-102: beats that brought it into play
    beats_live: int  # D-102: beats it has been active or offered


class CounterRow(Out):
    scope_ref: str
    key: str
    value: int
    created_day: int
    updated_day: int


class StateSections(Out):
    """The requested sections only (`?sections=`); a section not asked for is absent."""

    actors: list[ActorRow] | None = None
    claims: list[ClaimRow] | None = None
    beliefs: list[BeliefRow] | None = None
    places: list[PlaceRow] | None = None
    pcs: list[PCRow] | None = None
    sheets: list[SheetRow] | None = None
    items: list[ItemRow] | None = None
    factions: list[FactionRow] | None = None
    edges: list[EdgeRow] | None = None
    threads: list[ThreadRow] | None = None
    counters: list[CounterRow] | None = None


class WorldState(Out):
    branch: str | None = None  # absent for `?at=` (a commit, not a branch head)
    at: str | None  # the commit the state is read at
    state: StateSections


class CampaignState(Out):
    branch_id: str
    state: StateSections


class SectionDiff[R: BaseModel](Out):
    added: list[R]  # in b, not a
    removed: list[R]  # in a, not b
    changed: list[R]  # same key, different values (b's row)


class DiffSections(Out):
    """Only sections that differ are present."""

    actors: SectionDiff[ActorRow] | None = None
    claims: SectionDiff[ClaimRow] | None = None
    beliefs: SectionDiff[BeliefRow] | None = None
    places: SectionDiff[PlaceRow] | None = None
    pcs: SectionDiff[PCRow] | None = None
    sheets: SectionDiff[SheetRow] | None = None
    items: SectionDiff[ItemRow] | None = None
    factions: SectionDiff[FactionRow] | None = None
    edges: SectionDiff[EdgeRow] | None = None
    threads: SectionDiff[ThreadRow] | None = None
    counters: SectionDiff[CounterRow] | None = None


class Diff(Out):
    a: str
    b: str
    diff: DiffSections


# --- campaigns ---


class CampaignView(Campaign):
    """A campaign; `seed` is present for an operator only (a player can't predict combat)."""

    model_config = ConfigDict(extra="forbid")
    seed: int | None = None  # type: ignore[assignment]


class CampaignCreated(Out):
    campaign_id: str
    branch_id: str


class Joined(Out):
    actor_id: str
    token: str | None = None  # minted when the server keeps a token registry (D-39)


class Minted(Out):
    token: str


class Tokens(Out):
    tokens: list[TokenInfo]


class Revoked(Out):
    revoked: bool
    count: int | None = None  # how many, when revoking a participant's tokens
    closed_sockets: int


class Roster(Out):
    pcs: list[str]


class PacingOut(PacingReport):
    """`GET /campaigns/{c}/pacing` (D-95): each live thread's quiet beats + the next named one."""

    model_config = ConfigDict(extra="forbid")


class ThreadStateSet(Out):
    """`POST /campaigns/{c}/threads/{t}/state` (D-102)."""

    commit_id: str
    thread_id: str
    state: str


class TimeSkipped(Out):
    branch_id: str
    world_day: int


class DryRun(Out):
    events: list[DomainEvent]


class Consistency(Out):
    consistent: int
    total: int
    ratio: float


class Codex(Out):
    participant: str | None
    notes: list[ParticipantNote]


class Noted(Out):
    participant: str
    key: str
