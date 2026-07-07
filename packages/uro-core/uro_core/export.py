"""Export/import bundles with hash-chain verification (docs/03, 07). Portable — no DB.

A world's truth IS its event log. An export bundle carries the commits (each with its events)
+ branches + markers, stamped with a SELF-CONSISTENT hash chain (`commit_hash = h(parent_hash,
events)`, chained genesis→head). `verify_bundle` recomputes that chain and raises `ExportError`
on any mismatch — so a bundle altered in transit is caught before import (the trust anchor).

Note: because payloads round-trip through JSONB (which does not preserve key order), the bundle
re-derives its chain over the stored events at export time rather than copying the world's
in-DB commit hashes. The bundle is thus internally verifiable end-to-end (export→import); it is
tamper-evidence for the TRANSFER, which is what an export pack needs.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from uro_core.domain.events import CausedBy, DomainEvent, WorldTime
from uro_core.domain.hashing import compute_commit_hash
from uro_core.errors import ExportError


class BundleEvent(BaseModel):
    event_id: str
    seq: int
    event_type: str
    entity_refs: list[str] = Field(default_factory=list)
    world_time: dict[str, Any] = Field(default_factory=dict)
    caused_by: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)


class BundleCommit(BaseModel):
    commit_id: str
    parent_id: str | None
    depth: int
    commit_hash: str = ""  # stamped by chain_hashes at export; checked by verify_bundle
    events: list[BundleEvent] = Field(default_factory=list)


class BundleBranch(BaseModel):
    branch_id: str
    name: str
    head_commit: str | None
    forked_from: str | None = None


class BundleMarker(BaseModel):
    marker_id: str
    name: str
    commit_id: str


class WorldBundle(BaseModel):
    v: int = 1
    world_name: str
    commits: list[BundleCommit] = Field(default_factory=list)
    branches: list[BundleBranch] = Field(default_factory=list)
    markers: list[BundleMarker] = Field(default_factory=list)


def to_domain_event(e: BundleEvent) -> DomainEvent:
    """Reconstruct the DomainEvent whose canonical JSON feeds the commit hash."""
    return DomainEvent(
        event_id=e.event_id,
        event_type=e.event_type,
        entity_refs=list(e.entity_refs),
        world_time=WorldTime.model_validate(e.world_time),
        caused_by=CausedBy.model_validate(e.caused_by),
        payload=e.payload,
    )


def chain_hashes(commits: list[BundleCommit]) -> dict[str, str]:
    """Compute each commit's hash genesis→head (by depth), chaining parent→child."""
    hashes: dict[str, str] = {}
    for c in sorted(commits, key=lambda c: c.depth):
        parent_hash = hashes[c.parent_id] if c.parent_id and c.parent_id in hashes else None
        events = [to_domain_event(e) for e in sorted(c.events, key=lambda e: e.seq)]
        hashes[c.commit_id] = compute_commit_hash(parent_hash, events)
    return hashes


def stamp_chain(bundle: WorldBundle) -> None:
    """Fill in each commit's `commit_hash` from the recomputed chain (called at export)."""
    hashes = chain_hashes(bundle.commits)
    for c in bundle.commits:
        c.commit_hash = hashes[c.commit_id]


def verify_bundle(bundle: WorldBundle) -> None:
    """Recompute the chain and raise ExportError on the first mismatch (tamper-evident)."""
    hashes = chain_hashes(bundle.commits)
    for c in bundle.commits:
        if hashes[c.commit_id] != c.commit_hash:
            raise ExportError(
                f"hash-chain verification failed at commit {c.commit_id} "
                f"(depth {c.depth}) — the bundle was altered in transit"
            )
