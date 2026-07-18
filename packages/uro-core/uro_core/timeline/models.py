"""Timeline records (docs/03): worlds, branches, commits, campaigns.

Plain data records the store returns. Projections and snapshots arrive in Phase 1 and 2.
"""

from __future__ import annotations

from pydantic import BaseModel


class World(BaseModel):
    world_id: str
    name: str
    main_branch_id: str


class Branch(BaseModel):
    branch_id: str
    world_id: str
    name: str
    head_commit: str | None  # None only transiently; every branch is seeded at creation
    forked_from: str | None = None  # the commit this branch was forked at (None = main)


class BranchInfo(Branch):
    """A branch plus its head's depth — for `uro branch list`."""

    head_depth: int = 0


class Commit(BaseModel):
    commit_id: str
    world_id: str
    parent_id: str | None
    commit_hash: str
    depth: int = 0  # generation from genesis (genesis = 0)


class Marker(BaseModel):
    """A named, immutable ref to a commit (docs/03) — a tag, not an event."""

    marker_id: str
    world_id: str
    name: str
    commit_id: str


class LineageEntry(BaseModel):
    """One commit on a branch's lineage, for the git-log-style `uro log` view."""

    commit_id: str
    depth: int
    event_types: list[str]
    summary: str  # the beat's intent, or a terse event digest for non-beat commits
    markers: list[str]  # marker names anchored at this commit


class Campaign(BaseModel):
    campaign_id: str
    world_id: str
    branch_id: str
    ruleset_id: str = ""  # the ruleset this campaign is bound to (docs/06); "" → registry default
    ruleset_version: str = ""
    seed: int = 0  # mechanics RNG seed (docs/18 G-3) — makes a played campaign's combat replayable


# --- Projection read-models (docs/02, 07). Materialized state at a branch head. ---


class ActorView(BaseModel):
    actor_id: str
    name: str
    tier: int
    role: str
    aliases: list[str]
    status: str = "alive"  # alive | dead (docs/02; a death trace independent of the sheet)


class ClaimView(BaseModel):
    claim_id: str
    statement: str
    subject_refs: list[str]
    truth: str  # true | false | unknown
    origin: str
    created_day: int = 0  # in-fiction birth day (C5, D-34) — for rumor-age / expire_claims


class BeliefView(BaseModel):
    actor_id: str
    claim_id: str
    confidence: float
    learned_from: str | None


class PlaceView(BaseModel):
    place_id: str
    name: str
    kind: str  # region | settlement | site
    status: str  # active | destroyed
    description: str


class FactionView(BaseModel):
    faction_id: str
    name: str
    kind: str  # faction | religion
    description: str


class EdgeView(BaseModel):
    src: str
    rel_type: str
    dst: str
    weight: float


class ThreadView(BaseModel):
    thread_id: str
    stakes: str
    state: str  # dormant | offered | active
    provenance: str  # author | ai_backfill


class ParticipantNote(BaseModel):
    """A player's out-of-world meta-knowledge (docs/18 B8) — a note that belongs to the PARTICIPANT,
    not the branch, so it survives a fork/reset (time-loop / roguelike / NG+). It is NOT canon and
    NOT an NPC belief: it surfaces to the narrator only, as the player's private recollection."""

    key: str  # caller-supplied (or content-hashed) — the dedup key within (participant, world)
    text: str
    pinned: bool = False  # always surfaces (vs entity-triggered)
    entity_refs: list[str] = []  # e.g. ["name:vault"] — surfaces when the beat mentions them


class MemoryHit(BaseModel):
    text: str
    kind: str
    commit_id: str
    entity_refs: list[str]
    distance: float  # cosine distance to the query (lower = more similar)
