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


class Commit(BaseModel):
    commit_id: str
    world_id: str
    parent_id: str | None
    commit_hash: str


class Campaign(BaseModel):
    campaign_id: str
    world_id: str
    branch_id: str


# --- Projection read-models (docs/02, 07). Materialized state at a branch head. ---


class ActorView(BaseModel):
    actor_id: str
    name: str
    tier: int
    role: str
    aliases: list[str]


class ClaimView(BaseModel):
    claim_id: str
    statement: str
    subject_refs: list[str]
    truth: str  # true | false | unknown
    origin: str


class BeliefView(BaseModel):
    actor_id: str
    claim_id: str
    confidence: float
    learned_from: str | None
