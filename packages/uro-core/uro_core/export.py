"""Export/import bundles with hash-chain verification (docs/03, 07). Portable — no DB.

A world's truth IS its event log. An export bundle carries the commits (each with its events)
+ branches + markers, stamped with a SELF-CONSISTENT hash chain (`commit_hash = h(parent_hash,
events)`, chained genesis→head) plus a `manifest_hash` over the world name + branch/marker
structure + the commit hashes. `verify_bundle` recomputes both and raises `ExportError` on any
mismatch — so a bundle whose events, branch/marker structure, or name were altered in transit is
caught before import.

Scope, honestly: this is a KEYLESS integrity check — cheap tamper-EVIDENCE that catches accidental
corruption and naive edits (change one event and the recompute diverges). It is NOT cryptographic
AUTHENTICITY: a forger who recomputes the whole chain produces a self-consistent bundle. Binding
it to a signed root (an author key) is the export-pack hardening left for later.

Note: because payloads round-trip through JSONB (which does not preserve key order), the bundle
re-derives its chain over the stored events at export time rather than copying the world's in-DB
commit hashes — so it is internally verifiable end-to-end (export→import).
"""

from __future__ import annotations

import hashlib
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


class BundleEmbedding(BaseModel):
    content_hash: str
    vector: str  # pgvector text form '[...]' (content-hash-shared; never recomputed)


class BundleMemory(BaseModel):
    branch_id: str
    commit_id: str
    content_hash: str
    kind: str
    text: str
    entity_refs: list[str] = Field(default_factory=list)


class WorldBundle(BaseModel):
    v: int = 1
    world_name: str
    commits: list[BundleCommit] = Field(default_factory=list)
    branches: list[BundleBranch] = Field(default_factory=list)
    markers: list[BundleMarker] = Field(default_factory=list)
    # Semantic-memory cache (docs/07): carried so an import keeps long-range recall, symmetric
    # with copy-on-fork. Aux/best-effort — NOT part of manifest_hash (the authoritative log is).
    embeddings: list[BundleEmbedding] = Field(default_factory=list)
    memory: list[BundleMemory] = Field(default_factory=list)
    manifest_hash: str = ""  # binds world_name + branch/marker structure + commit hashes


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


def _manifest_digest(bundle: WorldBundle) -> str:
    """A digest over the bundle's name + branch/marker structure + commit hashes — binds the
    metadata the commit chain alone doesn't cover (a forged name or a repointed branch/marker)."""
    h = hashlib.sha256()
    h.update(bundle.world_name.encode("utf-8"))
    for c in sorted(bundle.commits, key=lambda c: c.commit_id):
        h.update(f"|c|{c.commit_id}|{c.commit_hash}".encode())
    for b in sorted(bundle.branches, key=lambda b: b.branch_id):
        h.update(f"|b|{b.branch_id}|{b.name}|{b.head_commit}|{b.forked_from}".encode())
    for m in sorted(bundle.markers, key=lambda m: m.marker_id):
        h.update(f"|m|{m.marker_id}|{m.name}|{m.commit_id}".encode())
    return h.hexdigest()


def stamp_chain(bundle: WorldBundle) -> None:
    """Fill in each commit's `commit_hash` + the manifest_hash from the recomputed chain."""
    hashes = chain_hashes(bundle.commits)
    for c in bundle.commits:
        c.commit_hash = hashes[c.commit_id]
    bundle.manifest_hash = _manifest_digest(bundle)


def verify_bundle(bundle: WorldBundle) -> None:
    """Recompute the chain + manifest digest; raise ExportError on the first mismatch."""
    hashes = chain_hashes(bundle.commits)
    for c in bundle.commits:
        if hashes[c.commit_id] != c.commit_hash:
            raise ExportError(
                f"hash-chain verification failed at commit {c.commit_id} "
                f"(depth {c.depth}) — the bundle was altered in transit"
            )
    if _manifest_digest(bundle) != bundle.manifest_hash:
        raise ExportError(
            "manifest verification failed — the world name or branch/marker structure was altered"
        )
