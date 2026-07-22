"""PostgreSQL EventStore adapter (docs/07, D-13, D-18).

asyncpg + hand-written SQL, no ORM. Implements the EventStore port. Lives in the
adapters ring — the core never imports this module (import-linter, docs/14).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import asyncpg

from uro_core.adapters.crypto import clean_secret, decrypt_secret, encrypt_secret
from uro_core.adapters.postgres.projector import (
    _SNAPSHOT_TABLES,
    apply_event,
    apply_raw,
    restore_snapshot,
    snapshot_state,
)
from uro_core.domain.events import (
    BeatResolvedPayload,
    DomainEvent,
    actor_created,
    adaptation_applied,
    campaign_ended,
    campaign_started,
    history_cause,
    item_created,
    pc_bound,
    pc_released,
    sheet_updated,
    time_advanced,
    world_genesis,
)
from uro_core.domain.extraction_policy import ExtractionPolicy
from uro_core.domain.hashing import compute_commit_hash
from uro_core.domain.ids import new_id
from uro_core.export import (
    BundleBranch,
    BundleCommit,
    BundleEmbedding,
    BundleEvent,
    BundleMarker,
    BundleMemory,
    WorldBundle,
    chain_hashes,
    stamp_chain,
    verify_bundle,
)
from uro_core.metering import LLMCall
from uro_core.ports.model_registry import ModelConnection, ProviderCredential, RoleBinding
from uro_core.timeline.models import (
    ActorView,
    BeliefView,
    Branch,
    BranchInfo,
    Campaign,
    ClaimView,
    Commit,
    EdgeView,
    FactionView,
    LineageEntry,
    Marker,
    MemoryHit,
    ParticipantNote,
    PlaceView,
    ThreadView,
    World,
)

# Full projection state is serialized every this-many commits (docs/03: N≈50) and
# always at markers. Materialization restores the nearest ancestor snapshot then
# replays forward, so this only trades snapshot storage for replay length — never
# correctness. Instance-overridable so tests can exercise the snapshot path cheaply.
SNAPSHOT_EVERY = 50

# Per-section PRIMARY KEY columns — how diff_branches matches "the same row" across two branches
# (docs/18 B5). A subset of each section's _SNAPSHOT_TABLES columns.
_SECTION_KEYS: dict[str, tuple[str, ...]] = {
    "actors": ("actor_id",),
    "claims": ("claim_id",),
    "beliefs": ("actor_id", "claim_id"),
    "places": ("place_id",),
    "pcs": ("campaign_id", "actor_id"),
    "sheets": ("actor_id",),
    "items": ("item_id",),
    "factions": ("faction_id",),
    "edges": ("src", "rel_type", "dst"),
    "threads": ("thread_id",),
    "counters": ("scope_ref", "key"),
}

# Fail-closed (docs/18 holistic review): query_across validates a section against _SNAPSHOT_TABLES,
# but diff_branches indexes _SECTION_KEYS after defaulting to the full _SNAPSHOT_TABLES catalog — so
# a future projection added to one dict but not the other would make diff KeyError instead of
# raising typo-loud. Keep them in lockstep at import: adding a projection forces adding its PK here.
assert set(_SECTION_KEYS) == set(_SNAPSHOT_TABLES), (
    "_SECTION_KEYS (diff PKs) and _SNAPSHOT_TABLES (fork/query catalog) drifted: "
    f"{set(_SECTION_KEYS) ^ set(_SNAPSHOT_TABLES)}"
)


# The campaign RNG seed (G-3) is stored in a signed BIGINT column; reject out-of-range values with a
# clean ValueError (→ CLI error / REST 400) instead of a raw asyncpg OverflowError at the INSERT.
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1


def _check_seed(seed: int) -> None:
    if not (_INT64_MIN <= seed <= _INT64_MAX):
        raise ValueError(f"seed {seed} out of range (must fit a signed 64-bit integer)")


def _vector_literal(vector: list[float]) -> str:
    """pgvector text form: '[0.1,0.2,...]' — passed as text and cast ::vector in SQL."""
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"


_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Shared prefix: a branch row plus its head commit's depth (BranchInfo).
_BRANCH_SELECT = (
    "SELECT b.branch_id, b.world_id, b.name, b.head_commit, b.forked_from, "
    "       c.depth AS head_depth "
    "FROM branches b JOIN commits c ON c.commit_id = b.head_commit "
)


def _lineage_entry(
    commit_id: str, depth: int, events: list[asyncpg.Record], markers: list[str]
) -> LineageEntry:
    """Fold one commit's events (seq-ordered) into a log line: the beat's intent if it
    is a beat, else a digest of its event types."""
    event_types = [e["event_type"] for e in events]
    summary = next(
        (e["payload"].get("intent_text", "") for e in events if e["event_type"] == "BeatResolved"),
        "",
    )
    if not summary:
        summary = ", ".join(event_types) if event_types else "(empty)"
    return LineageEntry(
        commit_id=commit_id,
        depth=depth,
        event_types=event_types,
        summary=summary,
        markers=markers,
    )


def _bundle_event(row: asyncpg.Record) -> BundleEvent:
    """One events-table row → the BundleEvent transport shape (BE-4 / export share it)."""
    return BundleEvent(
        event_id=row["event_id"],
        seq=row["seq"],
        event_type=row["event_type"],
        entity_refs=list(row["entity_refs"]),
        world_time=row["world_time"],
        caused_by=row["caused_by"],
        payload=row["payload"],
    )


async def _init_conn(conn: asyncpg.Connection) -> None:
    # asyncpg does not encode dict <-> jsonb automatically; register a codec.
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


class PostgresEventStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self._snapshot_every = SNAPSHOT_EVERY

    # --- lifecycle ---

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, init=_init_conn)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def dsn(self) -> str:
        return self._dsn

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("store not connected; call connect() first")
        return self._pool

    async def migrate(self) -> list[str]:
        """Apply pending numbered SQL migrations in order. Returns the ones applied."""
        applied: list[str] = []
        async with self.pool.acquire() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
            for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
                version = path.stem
                if version in done:
                    continue
                async with conn.transaction():
                    await conn.execute(path.read_text())
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES ($1)", version
                    )
                applied.append(version)
        return applied

    # --- worlds & campaigns ---

    async def create_world(
        self,
        name: str,
        *,
        tone: list[str] | None = None,
        prompt_overrides: dict[str, str] | None = None,
        ruleset_id: str = "",
        ruleset_version: str = "",
        rule_pack: dict[str, Any] | None = None,
        extra_events: list[DomainEvent] | None = None,
    ) -> World:
        """Create a world + its `main` branch. The genesis commit is `WorldGenesis` (carrying the
        pack's tone + prompt overrides + Reaction-Layer rule pack, docs/09, 17) plus any
        `extra_events` — world-pack import passes the authored seed events (emitter S) so authored
        geography/actors/factions exist as state before any History seeding."""
        if rule_pack:
            # Validate the Reaction-Layer pack LOUDLY at creation (gap-report Hollowloop G-6): a bad
            # pack must not persist and then die silently at the first beat (react()/agenda_tick
            # swallow load errors as a warning, disabling the whole pack). Fail here instead.
            from uro_core.worldpack.rules import RulePack

            RulePack(**rule_pack)  # raises on a bad version / unknown trigger / malformed action
        genesis = [
            world_genesis(
                name,
                tone=tone,
                prompt_overrides=prompt_overrides,
                ruleset_id=ruleset_id,
                ruleset_version=ruleset_version,
                rule_pack=rule_pack,
            ),
            *(extra_events or []),
        ]
        async with self.pool.acquire() as conn, conn.transaction():
            world_id = new_id()
            await conn.execute(
                "INSERT INTO worlds (world_id, name) VALUES ($1, $2)", world_id, name
            )
            commit_id = new_id()
            commit_hash = compute_commit_hash(None, genesis)
            await conn.execute(
                "INSERT INTO commits (commit_id, world_id, parent_id, commit_hash, depth) "
                "VALUES ($1, $2, NULL, $3, 0)",
                commit_id,
                world_id,
                commit_hash,
            )
            await self._insert_events(conn, commit_id, genesis)
            branch_id = new_id()
            await conn.execute(
                "INSERT INTO branches (branch_id, world_id, name, head_commit) "
                "VALUES ($1, $2, 'main', $3)",
                branch_id,
                world_id,
                commit_id,
            )
            # Project the seed events into the new main branch (genesis itself is a no-op).
            for event in genesis:
                await apply_event(conn, branch_id, event)
            return World(world_id=world_id, name=name, main_branch_id=branch_id)

    # --- export / import: a portable, hash-chain-verified world bundle (docs/03, 07, 08) ---

    async def export_world(self, world_id: str) -> WorldBundle:
        """Serialize a world's whole log — commits (with events), branches, markers — into a
        bundle stamped with a self-consistent hash chain (verifiable on import)."""
        # One transaction so a concurrent append_beat can't yield a torn bundle (a new commit
        # read without its events, or events without the branch head that points at them).
        async with self.pool.acquire() as conn, conn.transaction():
            world = await conn.fetchrow("SELECT name FROM worlds WHERE world_id = $1", world_id)
            if world is None:
                raise ValueError(f"no such world: {world_id}")
            commit_rows = await conn.fetch(
                "SELECT commit_id, parent_id, depth FROM commits "
                "WHERE world_id = $1 ORDER BY depth ASC",
                world_id,
            )
            commits: list[BundleCommit] = []
            for c in commit_rows:
                event_rows = await conn.fetch(
                    "SELECT event_id, seq, event_type, entity_refs, world_time, caused_by, payload "
                    "FROM events WHERE commit_id = $1 ORDER BY seq ASC",
                    c["commit_id"],
                )
                commits.append(
                    BundleCommit(
                        commit_id=c["commit_id"],
                        parent_id=c["parent_id"],
                        depth=c["depth"],
                        events=[
                            BundleEvent(
                                event_id=e["event_id"],
                                seq=e["seq"],
                                event_type=e["event_type"],
                                entity_refs=list(e["entity_refs"]),
                                world_time=e["world_time"],
                                caused_by=e["caused_by"],
                                payload=e["payload"],
                            )
                            for e in event_rows
                        ],
                    )
                )
            branch_rows = await conn.fetch(
                "SELECT branch_id, name, head_commit, forked_from FROM branches "
                "WHERE world_id = $1",
                world_id,
            )
            marker_rows = await conn.fetch(
                "SELECT marker_id, name, commit_id FROM markers WHERE world_id = $1", world_id
            )
            # Carry the semantic-memory cache so recall survives an import (symmetric with fork).
            branch_ids = [b["branch_id"] for b in branch_rows]
            memory_rows = await conn.fetch(
                "SELECT branch_id, commit_id, content_hash, kind, text, entity_refs "
                "FROM memory_index WHERE branch_id = ANY($1::text[])",
                branch_ids,
            )
            hashes = list({r["content_hash"] for r in memory_rows})
            emb_rows = (
                await conn.fetch(
                    "SELECT content_hash, vector::text AS vector FROM embeddings "
                    "WHERE content_hash = ANY($1::text[])",
                    hashes,
                )
                if hashes
                else []
            )
        bundle = WorldBundle(
            world_name=world["name"],
            commits=commits,
            branches=[BundleBranch(**dict(b)) for b in branch_rows],
            markers=[BundleMarker(**dict(m)) for m in marker_rows],
            embeddings=[BundleEmbedding(**dict(e)) for e in emb_rows],
            memory=[BundleMemory(**dict(m)) for m in memory_rows],
        )
        stamp_chain(bundle)  # self-consistent chain over the exported events
        return bundle

    async def import_world(self, bundle: WorldBundle) -> World:
        """Verify a bundle's hash chain, then instantiate it as a FRESH world (structural ids
        remapped so a same-store re-import is collision-safe). Projections are rebuilt by replay and
        the semantic-memory cache is re-inserted from the bundle (replay can't rebuild it) — so the
        world is immediately queryable, has long-range recall, and is continuable."""
        verify_bundle(bundle)  # ExportError if altered in transit
        commit_map = {c.commit_id: new_id() for c in bundle.commits}
        remapped = [
            BundleCommit(
                commit_id=commit_map[c.commit_id],
                parent_id=commit_map[c.parent_id] if c.parent_id else None,
                depth=c.depth,
                events=[
                    BundleEvent(
                        event_id=new_id(),
                        seq=e.seq,
                        event_type=e.event_type,
                        entity_refs=e.entity_refs,
                        world_time=e.world_time,
                        caused_by=e.caused_by,
                        payload=e.payload,
                    )
                    for e in c.events
                ],
            )
            for c in bundle.commits
        ]
        hashes = chain_hashes(remapped)  # fresh, valid chain for the new world
        async with self.pool.acquire() as conn, conn.transaction():
            world_id = new_id()
            await conn.execute(
                "INSERT INTO worlds (world_id, name) VALUES ($1, $2)", world_id, bundle.world_name
            )
            for c in sorted(remapped, key=lambda c: c.depth):
                await conn.execute(
                    "INSERT INTO commits (commit_id, world_id, parent_id, commit_hash, depth) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    c.commit_id,
                    world_id,
                    c.parent_id,
                    hashes[c.commit_id],
                    c.depth,
                )
                for e in c.events:
                    await conn.execute(
                        "INSERT INTO events (event_id, commit_id, seq, event_type, entity_refs, "
                        "world_time, caused_by, payload) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                        e.event_id,
                        c.commit_id,
                        e.seq,
                        e.event_type,
                        e.entity_refs,
                        e.world_time,
                        e.caused_by,
                        e.payload,
                    )
            branch_map: dict[str, str] = {}
            for b in bundle.branches:
                branch_map[b.branch_id] = new_id()
                await conn.execute(
                    "INSERT INTO branches (branch_id, world_id, name, head_commit, forked_from) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    branch_map[b.branch_id],
                    world_id,
                    b.name,
                    commit_map.get(b.head_commit) if b.head_commit else None,
                    commit_map.get(b.forked_from) if b.forked_from else None,
                )
            for m in bundle.markers:
                await conn.execute(
                    "INSERT INTO markers (marker_id, world_id, name, commit_id) "
                    "VALUES ($1, $2, $3, $4)",
                    new_id(),
                    world_id,
                    m.name,
                    commit_map[m.commit_id],
                )
            for b in bundle.branches:  # rebuild each branch's projections by replay
                if b.head_commit:
                    ancestry = await self._ancestry(conn, commit_map[b.head_commit])
                    await self._materialize_into(conn, branch_map[b.branch_id], ancestry)
            # Re-insert the semantic-memory cache (replay can't rebuild it — it needs the embedder).
            # Embeddings are content-hash-shared; membership rows carry the branch/commit remap.
            for emb in bundle.embeddings:
                await conn.execute(
                    "INSERT INTO embeddings (content_hash, vector) VALUES ($1, $2::vector) "
                    "ON CONFLICT (content_hash) DO NOTHING",
                    emb.content_hash,
                    emb.vector,
                )
            for mem in bundle.memory:
                await conn.execute(
                    "INSERT INTO memory_index "
                    "(memory_id, branch_id, content_hash, kind, text, entity_refs, commit_id) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    new_id(),
                    branch_map[mem.branch_id],
                    mem.content_hash,
                    mem.kind,
                    mem.text,
                    mem.entity_refs,
                    commit_map[mem.commit_id],
                )
            main = next((b for b in bundle.branches if b.name == "main"), bundle.branches[0])
            return World(
                world_id=world_id, name=bundle.world_name, main_branch_id=branch_map[main.branch_id]
            )

    async def get_world_by_name(self, name: str) -> World | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT w.world_id, w.name, b.branch_id AS main_branch_id "
                "FROM worlds w JOIN branches b "
                "  ON b.world_id = w.world_id AND b.name = 'main' "
                "WHERE w.name = $1",
                name,
            )
        return World(**dict(row)) if row else None

    async def create_campaign(self, world_id: str, branch_id: str, *, seed: int = 0) -> Campaign:
        _check_seed(seed)  # G-3: uniform with start_campaign — this entry point can pin a seed too
        campaign_id = new_id()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO campaigns (campaign_id, world_id, branch_id, seed) "
                "VALUES ($1, $2, $3, $4)",
                campaign_id,
                world_id,
                branch_id,
                seed,
            )
        return Campaign(campaign_id=campaign_id, world_id=world_id, branch_id=branch_id, seed=seed)

    async def get_campaign(self, campaign_id: str) -> Campaign | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT campaign_id, world_id, branch_id, ruleset_id, ruleset_version, seed "
                "FROM campaigns WHERE campaign_id = $1",
                campaign_id,
            )
        return Campaign(**dict(row)) if row else None

    # --- campaigns over branches, PC binding, time-skip (docs/02, 03, 12) ---

    async def start_campaign(
        self,
        world_id: str,
        branch_id: str,
        *,
        participant_id: str,
        adopt_actor_id: str | None = None,
        new_pc_name: str | None = None,
        new_pc_id: str | None = None,
        pc_sheet: dict[str, Any] | None = None,
        starting_items: list[str] | None = None,
        ruleset_id: str = "",
        ruleset_version: str = "",
        seed: int = 0,
    ) -> Campaign:
        """Create a campaign on a branch and bind its PC — either ADOPT an existing world
        actor (the meteor 'continue': play the one who caused it) or CREATE a fresh PC.
        Emits CampaignStarted + (ActorCreated if fresh) + PCBound (+ SheetUpdated if a
        ruleset-built `pc_sheet` is supplied) in one commit, so PC-ness and the character
        sheet are event-sourced and reproduced by materialization on any future fork. The
        caller (which holds the ruleset) builds the sheet; the store never interprets it."""
        if (adopt_actor_id is None) == (new_pc_name is None):
            raise ValueError("start_campaign needs exactly one of adopt_actor_id / new_pc_name")
        _check_seed(seed)  # G-3: the RNG seed lands in a BIGINT column — validate before the INSERT
        campaign_id = new_id()
        events: list[DomainEvent] = []
        if adopt_actor_id is not None:
            pc_actor_id = adopt_actor_id
        else:
            pc_actor_id = new_pc_id or new_id()
            events.append(
                actor_created(actor_id=pc_actor_id, name=new_pc_name or "", tier=2, role="pc")
            )
        events.append(
            campaign_started(
                campaign_id=campaign_id,
                branch_id=branch_id,
                party=[pc_actor_id],
                ruleset_id=ruleset_id,
                ruleset_version=ruleset_version,
                seed=seed,
            )
        )
        events.append(
            pc_bound(actor_id=pc_actor_id, participant_id=participant_id, campaign_id=campaign_id)
        )
        if pc_sheet is not None:
            events.append(
                sheet_updated(actor_id=pc_actor_id, sheet=pc_sheet, ruleset_id=ruleset_id)
            )
        # Starting equipment (docs/02) — so items exist in play and a lost fight can loot them.
        for name in starting_items or []:
            events.append(item_created(item_id=f"i:{new_id()}", name=name, owner_ref=pc_actor_id))
        async with self.pool.acquire() as conn, conn.transaction():
            branch = await conn.fetchrow(
                "SELECT world_id FROM branches WHERE branch_id = $1", branch_id
            )
            if branch is None or branch["world_id"] != world_id:
                raise KeyError(f"unknown branch {branch_id!r} in world {world_id!r}")
            if adopt_actor_id is not None:
                known = await conn.fetchval(
                    "SELECT 1 FROM proj_actors WHERE branch_id = $1 AND actor_id = $2",
                    branch_id,
                    adopt_actor_id,
                )
                if known is None:
                    raise ValueError(
                        f"cannot adopt unknown actor {adopt_actor_id!r} on this branch"
                    )
            await conn.execute(
                "INSERT INTO campaigns (campaign_id, world_id, branch_id, ruleset_id, "
                "ruleset_version, seed) VALUES ($1, $2, $3, $4, $5, $6)",
                campaign_id,
                world_id,
                branch_id,
                ruleset_id,
                ruleset_version,
                seed,
            )
            await self._append(conn, branch_id, events)
        return Campaign(
            campaign_id=campaign_id,
            world_id=world_id,
            branch_id=branch_id,
            ruleset_id=ruleset_id,
            ruleset_version=ruleset_version,
            seed=seed,
        )

    async def end_campaign(
        self, campaign_id: str, marker_name: str, *, outcome: str = ""
    ) -> Marker:
        """End a campaign: release its PCs (they revert to world NPCs) and mark the closing
        commit. Emits CampaignEnded + PCReleased(each active PC) in one commit, then a marker
        + snapshot at that head — the exact fork root a continuation branches from (docs/03)."""
        async with self.pool.acquire() as conn, conn.transaction():
            camp = await conn.fetchrow(
                "SELECT world_id, branch_id FROM campaigns WHERE campaign_id = $1", campaign_id
            )
            if camp is None:
                raise KeyError(f"unknown campaign {campaign_id!r}")
            world_id, branch_id = camp["world_id"], camp["branch_id"]
            pcs = await conn.fetch(
                "SELECT actor_id, participant_id FROM proj_pcs "
                "WHERE branch_id = $1 AND campaign_id = $2 AND active ORDER BY actor_id",
                branch_id,
                campaign_id,
            )
            events: list[DomainEvent] = [
                campaign_ended(campaign_id=campaign_id, outcome=outcome, marker_ref=marker_name)
            ]
            events.extend(
                pc_released(
                    actor_id=r["actor_id"],
                    participant_id=r["participant_id"],
                    campaign_id=campaign_id,
                )
                for r in pcs
            )
            commit = await self._append(conn, branch_id, events)
            marker_id = new_id()
            try:
                await conn.execute(
                    "INSERT INTO markers (marker_id, world_id, name, commit_id) "
                    "VALUES ($1, $2, $3, $4)",
                    marker_id,
                    world_id,
                    marker_name,
                    commit.commit_id,
                )
            except asyncpg.UniqueViolationError as exc:
                raise ValueError(f"marker {marker_name!r} already exists in this world") from exc
            await self._write_snapshot(conn, commit.commit_id, branch_id)
            return Marker(
                marker_id=marker_id,
                world_id=world_id,
                name=marker_name,
                commit_id=commit.commit_id,
            )

    async def time_skip(
        self, branch_id: str, days: int, *, reason: str = "time-skip on fork"
    ) -> Commit:
        """Advance in-fiction time on a branch (the fork's '50 years later'). Deterministic:
        commits TimeAdvanced + an AdaptationApplied HEADER — the PoC does no LLM ripple, so
        this records the skip honestly rather than pretending to regenerate threads (docs/03)."""
        if days <= 0:
            raise ValueError("time-skip days must be positive")
        async with self.pool.acquire() as conn, conn.transaction():
            locked = await conn.fetchval(
                "SELECT world_id FROM branches WHERE branch_id = $1 FOR UPDATE", branch_id
            )
            if locked is None:
                raise KeyError(f"unknown branch {branch_id!r}")
            from_day = await self._current_day(conn, branch_id)
            to_day = from_day + days
            events = [
                time_advanced(
                    from_day=from_day,
                    to_day=to_day,
                    reason=reason,
                    caused_by=history_cause("timeskip"),
                ),
                adaptation_applied(
                    scope="fork-timeskip",
                    summary=f"deterministic {days}-day skip; no LLM ripple in the PoC",
                    to_day=to_day,
                    caused_by=history_cause("adaptation"),
                ),
            ]
            return await self._append(conn, branch_id, events)

    async def is_pc(self, branch_id: str, actor_id: str) -> bool:
        """Per-branch PC-ness (docs/02): true iff the actor has an ACTIVE binding here."""
        async with self.pool.acquire() as conn:
            found = await conn.fetchval(
                "SELECT 1 FROM proj_pcs WHERE branch_id = $1 AND actor_id = $2 AND active LIMIT 1",
                branch_id,
                actor_id,
            )
        return found is not None

    async def active_pcs(self, branch_id: str) -> list[str]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT actor_id FROM proj_pcs WHERE branch_id = $1 AND active "
                "ORDER BY actor_id",
                branch_id,
            )
        return [r["actor_id"] for r in rows]

    async def campaign_pc(self, campaign_id: str) -> str | None:
        """The (first active) PC actor bound to a campaign — the SOLO fallback (docs/08). In a
        party (OQ-7) the pipeline resolves the ACTING participant's PC via `pc_for_participant`;
        this stays for single-participant play and as the fallback when a submitter is unbound."""
        async with self.pool.acquire() as conn:
            actor_id = await conn.fetchval(
                "SELECT p.actor_id FROM proj_pcs p "
                "JOIN campaigns c ON c.campaign_id = p.campaign_id AND c.branch_id = p.branch_id "
                "WHERE p.campaign_id = $1 AND p.active ORDER BY p.actor_id LIMIT 1",
                campaign_id,
            )
        return str(actor_id) if actor_id is not None else None

    async def pc_for_participant(self, campaign_id: str, participant_id: str) -> str | None:
        """The active PC a SPECIFIC participant drives (docs/08, OQ-7) — proj_pcs keyed on
        participant_id. This is how a party of N each get their own PC: a beat by participant P is
        planned/resolved as P's PC. None if P has no active binding (caller falls back to solo).
        Scoped to the campaign's OWN branch (like campaign_pc) so a fork-copied proj_pcs row on a
        sibling branch can't leak into another branch's resolution (cross-phase review P7-P2)."""
        async with self.pool.acquire() as conn:
            actor_id = await conn.fetchval(
                "SELECT p.actor_id FROM proj_pcs p "
                "JOIN campaigns c ON c.campaign_id = p.campaign_id AND c.branch_id = p.branch_id "
                "WHERE p.campaign_id = $1 AND p.participant_id = $2 AND p.active "
                "ORDER BY p.actor_id LIMIT 1",
                campaign_id,
                participant_id,
            )
        return str(actor_id) if actor_id is not None else None

    async def campaign_pcs(self, campaign_id: str) -> list[str]:
        """All active PC actor ids bound to a campaign, on its own branch (OQ-7). The engine uses
        the COUNT to decide an UNBOUND participant's fallback: 0 → a no-PC (Phase-1) campaign, act
        with no PC; 1 → safe solo fallback; ≥2 → a PARTY, where an unbound participant is refused
        rather than silently driving another player's PC (cross-phase review P7-P2/P3)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT p.actor_id FROM proj_pcs p "
                "JOIN campaigns c ON c.campaign_id = p.campaign_id AND c.branch_id = p.branch_id "
                "WHERE p.campaign_id = $1 AND p.active ORDER BY p.actor_id",
                campaign_id,
            )
        return [str(r["actor_id"]) for r in rows]

    async def pc_seats(self, campaign_id: str) -> list[str]:
        """Participant ids seated as active PCs, in DURABLE BIND ORDER (docs/18 B10, D-39) — the
        arbiter ring order, recovered from the PCBound/PCReleased log so it is stable across
        reconnect/restart (unlike live WS-connect race order) WITHOUT event-sourcing any turn
        state (D-31 kept). Why the log and not `campaign_pcs`: that (and `active_pcs`) uses
        `ORDER BY actor_id`, which mis-sorts ADOPTED PCs (an adopted actor's ULID reflects its
        original creation, not its binding), and snapshots serialize proj_pcs by all columns —
        both destroy insertion order. So we walk the events on the campaign's own branch ancestry
        (branch-scoped like `pc_for_participant`), ordered by (depth, seq): first-ever PCBound per
        participant fixes the seat; PCReleased drops them; a re-bind returns them to that seat."""
        async with self.pool.acquire() as conn:
            camp = await conn.fetchrow(
                "SELECT branch_id FROM campaigns WHERE campaign_id = $1", campaign_id
            )
            if camp is None:
                return []
            rows = await conn.fetch(
                """
                WITH RECURSIVE chain AS (
                    SELECT c.commit_id, c.parent_id, c.depth
                    FROM branches b JOIN commits c ON c.commit_id = b.head_commit
                    WHERE b.branch_id = $1
                    UNION ALL
                    SELECT c.commit_id, c.parent_id, c.depth
                    FROM commits c JOIN chain ON c.commit_id = chain.parent_id
                )
                SELECT e.event_type, e.payload->>'participant_id' AS participant_id
                FROM chain JOIN events e ON e.commit_id = chain.commit_id
                WHERE e.event_type IN ('PCBound', 'PCReleased')
                  AND e.payload->>'campaign_id' = $2
                ORDER BY chain.depth ASC, e.seq ASC
                """,
                camp["branch_id"],
                campaign_id,
            )
        order: list[str] = []  # first-ever PCBound order (the seat, never forgotten)
        active: set[str] = set()  # currently bound (a released participant leaves this set)
        for r in rows:
            pid = r["participant_id"]
            if pid is None:
                continue
            if r["event_type"] == "PCBound":
                if pid not in order:
                    order.append(pid)
                active.add(pid)
            else:  # PCReleased
                active.discard(pid)
        return [p for p in order if p in active]

    async def bind_pc(
        self,
        campaign_id: str,
        participant_id: str,
        *,
        adopt_actor_id: str | None = None,
        new_pc_name: str | None = None,
        new_pc_id: str | None = None,
        pc_sheet: dict[str, Any] | None = None,
        starting_items: list[str] | None = None,
        ruleset_id: str = "",
    ) -> str:
        """Seat an ADDITIONAL participant's PC on a LIVE campaign (docs/08, OQ-7) — the party-join
        path `start_campaign` only did for the first participant. Adopt an existing branch actor or
        create a fresh PC; emits (ActorCreated if fresh) + PCBound (+ SheetUpdated + starting items)
        in one commit. Idempotent: returns the existing PC if the participant is already bound.
        Returns the bound PC actor_id."""
        existing = await self.pc_for_participant(campaign_id, participant_id)
        if existing is not None:
            return existing  # already seated — a re-join is a no-op
        if (adopt_actor_id is None) == (new_pc_name is None):
            raise ValueError("bind_pc needs exactly one of adopt_actor_id / new_pc_name")
        campaign = await self.get_campaign(campaign_id)
        if campaign is None:
            raise KeyError(f"unknown campaign {campaign_id!r}")
        branch_id = campaign.branch_id
        events: list[DomainEvent] = []
        if adopt_actor_id is not None:
            pc_actor_id = adopt_actor_id
        else:
            pc_actor_id = new_pc_id or new_id()
            events.append(
                actor_created(actor_id=pc_actor_id, name=new_pc_name or "", tier=2, role="pc")
            )
        events.append(
            pc_bound(actor_id=pc_actor_id, participant_id=participant_id, campaign_id=campaign_id)
        )
        if pc_sheet is not None:
            events.append(
                sheet_updated(
                    actor_id=pc_actor_id,
                    sheet=pc_sheet,
                    ruleset_id=ruleset_id or campaign.ruleset_id,
                )
            )
        for name in starting_items or []:
            events.append(item_created(item_id=f"i:{new_id()}", name=name, owner_ref=pc_actor_id))
        async with self.pool.acquire() as conn, conn.transaction():
            if adopt_actor_id is not None:
                known = await conn.fetchval(
                    "SELECT 1 FROM proj_actors WHERE branch_id = $1 AND actor_id = $2",
                    branch_id,
                    adopt_actor_id,
                )
                if known is None:
                    raise ValueError(f"cannot adopt unknown actor {adopt_actor_id!r} on branch")
                # Do not let a second participant adopt an actor already an ACTIVE PC: the
                # projector's actor-keyed PCBound upsert would silently reassign it, clobbering
                # the first participant's binding (P7-P2 review). Two players can't share a PC.
                taken = await conn.fetchval(
                    "SELECT 1 FROM proj_pcs WHERE branch_id = $1 AND actor_id = $2 AND active",
                    branch_id,
                    adopt_actor_id,
                )
                if taken is not None:
                    raise ValueError(
                        f"actor {adopt_actor_id!r} is already an active PC — pick another to adopt"
                    )
            await self._append(conn, branch_id, events)
        return pc_actor_id

    async def current_world_time(self, branch_id: str) -> int:
        """The branch's latest in-fiction day (max over its ancestry; 0 if unset)."""
        async with self.pool.acquire() as conn:
            return await self._current_day(conn, branch_id)

    async def current_world_time_batch(self, branch_ids: list[str]) -> dict[str, int]:
        """The in-fiction day for many branches at once (docs/18 B5 - a cross-branch read).
        One recursive walk seeded from every requested head, carrying its origin branch, so a
        branch tree costs one round-trip instead of one per branch. Branches with no world_time
        events are absent from the result; callers default them to 0. Empty input -> empty dict."""
        if not branch_ids:
            return {}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH RECURSIVE chain AS (
                    SELECT b.branch_id AS origin, c.commit_id, c.parent_id
                    FROM branches b JOIN commits c ON c.commit_id = b.head_commit
                    WHERE b.branch_id = ANY($1::text[])
                    UNION ALL
                    SELECT chain.origin, c.commit_id, c.parent_id
                    FROM commits c JOIN chain ON c.commit_id = chain.parent_id
                )
                SELECT origin, max((e.world_time->>'day')::int) AS day
                FROM chain JOIN events e ON e.commit_id = chain.commit_id
                GROUP BY origin
                """,
                branch_ids,
            )
        return {r["origin"]: int(r["day"]) for r in rows if r["day"] is not None}

    @staticmethod
    async def _current_day(conn: asyncpg.Connection, branch_id: str) -> int:
        day = await conn.fetchval(
            """
            WITH RECURSIVE chain AS (
                SELECT c.commit_id, c.parent_id
                FROM branches b JOIN commits c ON c.commit_id = b.head_commit
                WHERE b.branch_id = $1
                UNION ALL
                SELECT c.commit_id, c.parent_id
                FROM commits c JOIN chain ON c.commit_id = chain.parent_id
            )
            SELECT max((e.world_time->>'day')::int)
            FROM chain JOIN events e ON e.commit_id = chain.commit_id
            """,
            branch_id,
        )
        return int(day) if day is not None else 0

    # --- the timeline ---

    async def append_beat(self, branch_id: str, events: list[DomainEvent]) -> Commit:
        async with self.pool.acquire() as conn, conn.transaction():
            return await self._append(conn, branch_id, events)

    async def _append(
        self, conn: asyncpg.Connection, branch_id: str, events: list[DomainEvent]
    ) -> Commit:
        """The commit core, on a caller-provided connection/transaction — so a campaign
        start or a time-skip can commit its events atomically alongside its own writes."""
        branch = await conn.fetchrow(
            "SELECT world_id, head_commit FROM branches WHERE branch_id = $1 FOR UPDATE",
            branch_id,
        )
        if branch is None:
            raise KeyError(f"unknown branch {branch_id!r}")
        parent_id = branch["head_commit"]
        parent_hash = None
        parent_depth = -1  # so genesis's child (parent_depth 0) lands at depth 1
        if parent_id is not None:
            parent = await conn.fetchrow(
                "SELECT commit_hash, depth FROM commits WHERE commit_id = $1", parent_id
            )
            parent_hash, parent_depth = parent["commit_hash"], parent["depth"]
        commit_id = new_id()
        commit_hash = compute_commit_hash(parent_hash, events)
        depth = parent_depth + 1
        await conn.execute(
            "INSERT INTO commits (commit_id, world_id, parent_id, commit_hash, depth) "
            "VALUES ($1, $2, $3, $4, $5)",
            commit_id,
            branch["world_id"],
            parent_id,
            commit_hash,
            depth,
        )
        await self._insert_events(conn, commit_id, events)
        # Project in the SAME transaction — projections never drift from the log.
        for event in events:
            await apply_event(conn, branch_id, event)
        await conn.execute(
            "UPDATE branches SET head_commit = $1 WHERE branch_id = $2",
            commit_id,
            branch_id,
        )
        # Periodic snapshot: the branch's proj_* rows now equal state at this new
        # head, and state-at-a-commit is branch-independent — capture it verbatim.
        if depth > 0 and depth % self._snapshot_every == 0:
            await self._write_snapshot(conn, commit_id, branch_id)
        return Commit(
            commit_id=commit_id,
            world_id=branch["world_id"],
            parent_id=parent_id,
            commit_hash=commit_hash,
            depth=depth,
        )

    async def recent_beats(self, branch_id: str, limit: int) -> list[BeatResolvedPayload]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH RECURSIVE chain AS (
                    SELECT c.commit_id, c.parent_id, 0 AS depth
                    FROM branches b JOIN commits c ON c.commit_id = b.head_commit
                    WHERE b.branch_id = $1
                    UNION ALL
                    SELECT c.commit_id, c.parent_id, chain.depth + 1
                    FROM commits c JOIN chain ON c.commit_id = chain.parent_id
                )
                SELECT e.payload
                FROM chain
                JOIN events e ON e.commit_id = chain.commit_id
                WHERE e.event_type = 'BeatResolved'
                ORDER BY chain.depth ASC, e.seq DESC
                LIMIT $2
                """,
                branch_id,
                limit,
            )
        # rows are newest-first; present oldest-first for chat reconstruction.
        return [BeatResolvedPayload(**r["payload"]) for r in reversed(rows)]

    # --- branching: markers, snapshots, fork, materialization (docs/03, 07) ---

    async def get_world(self, world_id: str) -> World | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT w.world_id, w.name, b.branch_id AS main_branch_id "
                "FROM worlds w JOIN branches b "
                "  ON b.world_id = w.world_id AND b.name = 'main' "
                "WHERE w.world_id = $1",
                world_id,
            )
        return World(**dict(row)) if row else None

    async def list_worlds(self) -> list[World]:
        """All worlds (docs/18 B3 — a consumer's lobby needs discovery). Ordered by id."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT w.world_id, w.name, b.branch_id AS main_branch_id "
                "FROM worlds w JOIN branches b "
                "  ON b.world_id = w.world_id AND b.name = 'main' ORDER BY w.world_id"
            )
        return [World(**dict(r)) for r in rows]

    async def list_campaigns(self, world_id: str | None = None) -> list[Campaign]:
        """All campaigns, optionally scoped to a world (docs/18 B3). Ordered by id."""
        async with self.pool.acquire() as conn:
            base = (
                "SELECT campaign_id, world_id, branch_id, ruleset_id, ruleset_version, seed "
                "FROM campaigns"
            )
            if world_id is None:
                rows = await conn.fetch(base + " ORDER BY campaign_id")
            else:
                rows = await conn.fetch(
                    base + " WHERE world_id = $1 ORDER BY campaign_id", world_id
                )
        return [Campaign(**dict(r)) for r in rows]

    async def get_branch(self, branch_id: str) -> BranchInfo | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(_BRANCH_SELECT + "WHERE b.branch_id = $1", branch_id)
        return BranchInfo(**dict(row)) if row else None

    async def get_branch_by_name(self, world_id: str, name: str) -> BranchInfo | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                _BRANCH_SELECT + "WHERE b.world_id = $1 AND b.name = $2 ORDER BY head_depth DESC",
                world_id,
                name,
            )
        return BranchInfo(**dict(row)) if row else None

    async def list_branches(self, world_id: str) -> list[BranchInfo]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                _BRANCH_SELECT + "WHERE b.world_id = $1 ORDER BY head_depth ASC, b.name ASC",
                world_id,
            )
        return [BranchInfo(**dict(r)) for r in rows]

    async def create_marker(self, world_id: str, name: str, branch_id: str) -> Marker:
        """Mark a branch's current head with a name (docs/03). Also snapshots that
        commit — markers are the guaranteed snapshot points a fork can root from."""
        async with self.pool.acquire() as conn, conn.transaction():
            # FOR UPDATE serializes against concurrent append_beat on this branch (which
            # also locks the row) — otherwise, under READ COMMITTED, an append could advance
            # the head between this read and snapshot_state below, storing state-at-Y under X.
            head = await conn.fetchval(
                "SELECT head_commit FROM branches WHERE branch_id = $1 AND world_id = $2 "
                "FOR UPDATE",
                branch_id,
                world_id,
            )
            if head is None:
                raise KeyError(f"unknown branch {branch_id!r} in world {world_id!r}")
            marker_id = new_id()
            try:
                await conn.execute(
                    "INSERT INTO markers (marker_id, world_id, name, commit_id) "
                    "VALUES ($1, $2, $3, $4)",
                    marker_id,
                    world_id,
                    name,
                    head,
                )
            except asyncpg.UniqueViolationError as exc:
                raise ValueError(f"marker {name!r} already exists in this world") from exc
            await self._write_snapshot(conn, head, branch_id)
            return Marker(marker_id=marker_id, world_id=world_id, name=name, commit_id=head)

    async def list_markers(self, world_id: str) -> list[Marker]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT marker_id, world_id, name, commit_id FROM markers "
                "WHERE world_id = $1 ORDER BY created_at ASC, name ASC",
                world_id,
            )
        return [Marker(**dict(r)) for r in rows]

    async def resolve_ref(self, world_id: str, ref: str) -> str:
        """A marker name or a raw commit_id → a commit_id. Markers win on collision."""
        async with self.pool.acquire() as conn:
            return await self._resolve_ref(conn, world_id, ref)

    async def fork_branch(self, world_id: str, from_ref: str, name: str) -> Branch:
        """Branch from any commit (docs/03). The new ref shares history up to `from_ref`;
        its projections are materialized there (copy-on-fork), and memory-index rows in
        that ancestry are copied to point at the shared embeddings. New commits chain off
        `from_ref`, so sibling branches never see each other's post-fork events."""
        async with self.pool.acquire() as conn, conn.transaction():
            from_commit = await self._resolve_ref(conn, world_id, from_ref)
            new_branch_id = new_id()
            try:
                await conn.execute(
                    "INSERT INTO branches (branch_id, world_id, name, head_commit, forked_from) "
                    "VALUES ($1, $2, $3, $4, $4)",
                    new_branch_id,
                    world_id,
                    name,
                    from_commit,
                )
            except asyncpg.UniqueViolationError as exc:
                # Branch names are unique per world (git-like); 'main' is always taken.
                raise ValueError(f"branch {name!r} already exists in this world") from exc
            ancestry = await self._ancestry(conn, from_commit)
            await self._materialize_into(conn, new_branch_id, ancestry)
            await self._copy_memory(conn, new_branch_id, [c["commit_id"] for c in ancestry])
            return Branch(
                branch_id=new_branch_id,
                world_id=world_id,
                name=name,
                head_commit=from_commit,
                forked_from=from_commit,
            )

    async def lineage(self, branch_id: str, limit: int = 50) -> list[LineageEntry]:
        """A branch's commit lineage, head→genesis — the `uro log` view. Per-branch
        lineage only; branches don't merge, so this never crosses into a sibling (docs/02)."""
        async with self.pool.acquire() as conn:
            commits = await conn.fetch(
                """
                WITH RECURSIVE chain AS (
                    SELECT c.commit_id, c.parent_id, c.depth
                    FROM branches b JOIN commits c ON c.commit_id = b.head_commit
                    WHERE b.branch_id = $1
                    UNION ALL
                    SELECT c.commit_id, c.parent_id, c.depth
                    FROM commits c JOIN chain ON c.commit_id = chain.parent_id
                )
                SELECT commit_id, depth FROM chain ORDER BY depth DESC LIMIT $2
                """,
                branch_id,
                limit,
            )
            ids = [r["commit_id"] for r in commits]
            if not ids:
                return []
            events = await conn.fetch(
                "SELECT commit_id, seq, event_type, payload FROM events "
                "WHERE commit_id = ANY($1::text[]) ORDER BY commit_id, seq",
                ids,
            )
            markers = await conn.fetch(
                "SELECT commit_id, name FROM markers WHERE commit_id = ANY($1::text[])",
                ids,
            )
        by_commit: dict[str, list[asyncpg.Record]] = {}
        for e in events:
            by_commit.setdefault(e["commit_id"], []).append(e)
        marks: dict[str, list[str]] = {}
        for m in markers:
            marks.setdefault(m["commit_id"], []).append(m["name"])
        return [
            _lineage_entry(
                c["commit_id"],
                c["depth"],
                by_commit.get(c["commit_id"], []),
                marks.get(c["commit_id"], []),
            )
            for c in commits
        ]

    async def commit_detail(self, world_id: str, commit_id: str) -> BundleCommit | None:
        """One commit's metadata + its ordered events, world-scoped (BE-4). None if the commit is
        not in this world. Reuses the export commit/event transport shapes."""
        async with self.pool.acquire() as conn:
            c = await conn.fetchrow(
                "SELECT commit_id, parent_id, depth, commit_hash FROM commits "
                "WHERE commit_id = $1 AND world_id = $2",
                commit_id,
                world_id,
            )
            if c is None:
                return None
            event_rows = await conn.fetch(
                "SELECT event_id, seq, event_type, entity_refs, world_time, caused_by, payload "
                "FROM events WHERE commit_id = $1 ORDER BY seq ASC",
                commit_id,
            )
        return BundleCommit(
            commit_id=c["commit_id"],
            parent_id=c["parent_id"],
            depth=c["depth"],
            commit_hash=c["commit_hash"],
            events=[_bundle_event(e) for e in event_rows],
        )

    async def branch_events(
        self,
        branch_id: str,
        *,
        event_type: str | None = None,
        entity_ref: str | None = None,
        caused_by: str | None = None,
        limit: int = 50,
    ) -> list[BundleEvent]:
        """Raw events along a branch's lineage (head→genesis), optionally filtered. Reuses the
        `lineage` recursive walk; a NULL filter arg is a no-op (SQL `$n IS NULL OR …`)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH RECURSIVE chain AS (
                    SELECT c.commit_id, c.parent_id, c.depth
                    FROM branches b JOIN commits c ON c.commit_id = b.head_commit
                    WHERE b.branch_id = $1
                    UNION ALL
                    SELECT c.commit_id, c.parent_id, c.depth
                    FROM commits c JOIN chain ON c.commit_id = chain.parent_id
                )
                SELECT e.event_id, e.seq, e.event_type, e.entity_refs, e.world_time,
                       e.caused_by, e.payload, ch.depth
                FROM events e JOIN chain ch ON e.commit_id = ch.commit_id
                WHERE ($2::text IS NULL OR e.event_type = $2)
                  AND ($3::text IS NULL OR $3 = ANY(e.entity_refs))
                  AND ($4::text IS NULL OR e.caused_by->>'kind' = $4)
                ORDER BY ch.depth DESC, e.seq DESC
                LIMIT $5
                """,
                branch_id,
                event_type,
                entity_ref,
                caused_by,
                limit,
            )
        return [_bundle_event(r) for r in rows]

    # --- places projection (docs/02); used by the meteor test and `uro log` ---

    async def get_place(self, branch_id: str, place_id: str) -> PlaceView | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT place_id, name, kind, status, description FROM proj_places "
                "WHERE branch_id = $1 AND place_id = $2",
                branch_id,
                place_id,
            )
        return PlaceView(**dict(row)) if row else None

    async def list_places(self, branch_id: str) -> list[PlaceView]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT place_id, name, kind, status, description FROM proj_places "
                "WHERE branch_id = $1 ORDER BY place_id",
                branch_id,
            )
        return [PlaceView(**dict(r)) for r in rows]

    async def list_factions(self, branch_id: str) -> list[FactionView]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT faction_id, name, kind, description FROM proj_factions "
                "WHERE branch_id = $1 ORDER BY faction_id",
                branch_id,
            )
        return [FactionView(**dict(r)) for r in rows]

    async def get_faction(self, branch_id: str, faction_id: str) -> FactionView | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT faction_id, name, kind, description FROM proj_factions "
                "WHERE branch_id = $1 AND faction_id = $2",
                branch_id,
                faction_id,
            )
        return FactionView(**dict(row)) if row else None

    async def list_edges(self, branch_id: str, rel_type: str | None = None) -> list[EdgeView]:
        """The graph's edges on a branch (docs/07), optionally filtered by relation type."""
        async with self.pool.acquire() as conn:
            if rel_type is None:
                rows = await conn.fetch(
                    "SELECT src, rel_type, dst, weight FROM proj_edges "
                    "WHERE branch_id = $1 ORDER BY src, rel_type, dst",
                    branch_id,
                )
            else:
                rows = await conn.fetch(
                    "SELECT src, rel_type, dst, weight FROM proj_edges "
                    "WHERE branch_id = $1 AND rel_type = $2 ORDER BY src, dst",
                    branch_id,
                    rel_type,
                )
        return [EdgeView(**dict(r)) for r in rows]

    async def list_threads(self, branch_id: str) -> list[ThreadView]:
        """A branch's conflict-seed threads (docs/09), authored + AI-backfilled (provenance)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT thread_id, stakes, state, provenance FROM proj_threads "
                "WHERE branch_id = $1 ORDER BY thread_id",
                branch_id,
            )
        return [ThreadView(**dict(r)) for r in rows]

    async def get_counter(self, branch_id: str, scope_ref: str, key: str) -> int:
        """A Computation-Layer integer counter (docs/19, D-34); 0 when absent."""
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT value FROM proj_counters "
                "WHERE branch_id = $1 AND scope_ref = $2 AND key = $3",
                branch_id,
                scope_ref,
                key,
            )
        return int(value) if value is not None else 0

    async def list_counters(self, branch_id: str, scope_ref: str) -> list[tuple[str, int]]:
        """All (key, value) counters on a scope entity, ordered by key."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value FROM proj_counters WHERE branch_id = $1 AND scope_ref = $2 "
                "ORDER BY key",
                branch_id,
                scope_ref,
            )
        return [(r["key"], int(r["value"])) for r in rows]

    # --- participant memory (docs/18 B8): NOT branch-scoped, NOT a projection, NOT copied by a
    # fork — a player's out-of-world notes keyed on (participant_id, world_ref). ---

    async def participant_remember(
        self,
        participant_id: str,
        world_ref: str,
        text: str,
        *,
        key: str | None = None,
        pinned: bool = False,
        entity_refs: list[str] | None = None,
    ) -> str:
        note_key = key or hashlib.sha256(text.strip().encode()).hexdigest()[:16]
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO participant_notes (participant_id, world_ref, key, text, pinned, "
                "entity_refs) VALUES ($1, $2, $3, $4, $5, $6) "
                "ON CONFLICT (participant_id, world_ref, key) DO UPDATE SET "
                "text = EXCLUDED.text, pinned = EXCLUDED.pinned, "
                "entity_refs = EXCLUDED.entity_refs",
                participant_id,
                world_ref,
                note_key,
                text,
                pinned,
                entity_refs or [],
            )
        return note_key

    async def participant_notes(self, participant_id: str, world_ref: str) -> list[ParticipantNote]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, text, pinned, entity_refs FROM participant_notes "
                "WHERE participant_id = $1 AND world_ref = $2 ORDER BY key",
                participant_id,
                world_ref,
            )
        return [
            ParticipantNote(
                key=r["key"], text=r["text"], pinned=r["pinned"], entity_refs=list(r["entity_refs"])
            )
            for r in rows
        ]

    # --- Session tokens (docs/18 B10, D-39): a durable, hashed, revocable registry, OFF the branch
    # axis (like participant_notes) — never a proj_*, never event-sourced, never forked. Only
    # sha256(token) reaches the store; the plaintext is minted server-side and never persisted. ---

    async def mint_token(self, token_hash: str, participant_id: str, campaign_id: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO session_tokens (token_hash, participant_id, campaign_id) "
                "VALUES ($1, $2, $3) ON CONFLICT (token_hash) DO UPDATE SET "
                "participant_id = EXCLUDED.participant_id, campaign_id = EXCLUDED.campaign_id, "
                "revoked = false",
                token_hash,
                participant_id,
                campaign_id,
            )

    async def revoke_token(self, token_hash: str) -> bool:
        async with self.pool.acquire() as conn:
            # Only report True when a LIVE token was actually revoked (not an unknown/already-
            # revoked hash) — the endpoint uses this to distinguish a real revoke from a no-op.
            row = await conn.fetchrow(
                "UPDATE session_tokens SET revoked = true "
                "WHERE token_hash = $1 AND revoked = false RETURNING token_hash",
                token_hash,
            )
        return row is not None

    async def list_session_tokens(self) -> list[tuple[str, str, str]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT token_hash, participant_id, campaign_id "
                "FROM session_tokens WHERE NOT revoked"
            )
        return [(r["token_hash"], r["participant_id"], r["campaign_id"]) for r in rows]

    # --- Model-connection registry (D-47, docs/20) — instance-level, OFF THE BRANCH/EVENT AXIS ---

    async def add_credential(
        self,
        *,
        provider: str,
        access_token: str | None,
        refresh_token: str | None = None,
        auth_mode: str = "api_key",
    ) -> str:
        # Reject a control-char (CR/LF) in a secret: never valid in an HTTP header, and if stored it
        # makes httpx raise `Illegal header value b'Bearer sk-…'` at request-build — whose text
        # carries the PLAINTEXT KEY into the refresh/test error surfaces (review HIGH). Strip
        # surrounding whitespace (routine copy-paste) and refuse embedded CR/LF loudly at ingestion.
        access_token = clean_secret(access_token)
        refresh_token = clean_secret(refresh_token)
        # Encrypt BEFORE the DB ever sees it (app-level Fernet, env KEK) — no plaintext at rest.
        enc_access = encrypt_secret(access_token) if access_token is not None else None
        enc_refresh = encrypt_secret(refresh_token) if refresh_token is not None else None
        cid = new_id()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO provider_credentials (id, provider, access_token, refresh_token, "
                "auth_mode) VALUES ($1, $2, $3, $4, $5)",
                cid,
                provider,
                enc_access,
                enc_refresh,
                auth_mode,
            )
        return cid

    async def list_credentials(self) -> list[ProviderCredential]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, provider, auth_mode, last_refresh, "
                "access_token IS NOT NULL AS has_access, refresh_token IS NOT NULL AS has_refresh "
                "FROM provider_credentials ORDER BY created_at"
            )
        return [
            ProviderCredential(
                id=r["id"],
                provider=r["provider"],
                auth_mode=r["auth_mode"],
                has_access_token=r["has_access"],
                has_refresh_token=r["has_refresh"],
                last_refresh=r["last_refresh"],
            )
            for r in rows
        ]

    async def delete_credential(self, credential_id: str) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM provider_credentials WHERE id = $1 RETURNING id", credential_id
            )
        return row is not None

    async def get_secret(self, credential_id: str) -> tuple[str | None, str | None] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT access_token, refresh_token FROM provider_credentials WHERE id = $1",
                credential_id,
            )
        if row is None:
            return None
        access = decrypt_secret(row["access_token"]) if row["access_token"] is not None else None
        refresh = decrypt_secret(row["refresh_token"]) if row["refresh_token"] is not None else None
        return access, refresh

    async def update_credential_tokens(
        self, credential_id: str, *, access_token: str, refresh_token: str | None = None
    ) -> None:
        # Same ingestion hygiene as add_credential: refuse embedded CR/LF, strip surrounding
        # whitespace, then Fernet-encrypt before the DB sees the rotated tokens.
        access_token = clean_secret(access_token) or ""
        cleaned_refresh = clean_secret(refresh_token)
        enc_access = encrypt_secret(access_token)
        enc_refresh = encrypt_secret(cleaned_refresh) if cleaned_refresh is not None else None
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE provider_credentials SET access_token = $2, "
                "refresh_token = COALESCE($3, refresh_token), last_refresh = now(), "
                "updated_at = now() WHERE id = $1",
                credential_id,
                enc_access,
                enc_refresh,
            )

    # --- extraction policy (D-49): instance-level, off the event/branch axis ---
    async def get_extraction_policy(self) -> ExtractionPolicy:
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT extract_actors, extract_places, extract_claims "
                    "FROM extraction_policy WHERE id = 'singleton'"
                )
        except asyncpg.UndefinedTableError:
            return ExtractionPolicy()  # pre-migration DB → all-on default (unchanged behavior)
        if row is None:
            return ExtractionPolicy()
        return ExtractionPolicy(
            extract_actors=row["extract_actors"],
            extract_places=row["extract_places"],
            extract_claims=row["extract_claims"],
        )

    async def set_extraction_policy(self, policy: ExtractionPolicy) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO extraction_policy "
                "(id, extract_actors, extract_places, extract_claims, updated_at) "
                "VALUES ('singleton', $1, $2, $3, now()) "
                "ON CONFLICT (id) DO UPDATE SET extract_actors = $1, extract_places = $2, "
                "extract_claims = $3, updated_at = now()",
                policy.extract_actors,
                policy.extract_places,
                policy.extract_claims,
            )

    @staticmethod
    def _connection_row(r: asyncpg.Record) -> ModelConnection:
        return ModelConnection(
            id=r["id"],
            name=r["name"],
            provider=r["provider"],
            base_url=r["base_url"],
            auth_id=r["auth_id"],
            is_enabled=r["is_enabled"],
            cached_models=r["cached_models"],  # jsonb codec → a list (or None)
        )

    async def add_connection(
        self,
        *,
        name: str,
        provider: str,
        base_url: str | None = None,
        auth_id: str | None = None,
    ) -> str:
        cid = new_id()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO model_connections (id, name, provider, base_url, auth_id) "
                "VALUES ($1, $2, $3, $4, $5)",
                cid,
                name,
                provider,
                base_url,
                auth_id,
            )
        return cid

    async def list_connections(self) -> list[ModelConnection]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, provider, base_url, auth_id, is_enabled, cached_models "
                "FROM model_connections ORDER BY created_at"
            )
        return [self._connection_row(r) for r in rows]

    async def get_connection(self, connection_id: str) -> ModelConnection | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, name, provider, base_url, auth_id, is_enabled, cached_models "
                "FROM model_connections WHERE id = $1",
                connection_id,
            )
        return self._connection_row(row) if row is not None else None

    async def set_connection_enabled(self, connection_id: str, enabled: bool) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE model_connections SET is_enabled = $2, updated_at = now() "
                "WHERE id = $1 RETURNING id",
                connection_id,
                enabled,
            )
        return row is not None

    async def set_connection_models(self, connection_id: str, models: list[dict[str, str]]) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE model_connections SET cached_models = $2, updated_at = now() "
                "WHERE id = $1 RETURNING id",
                connection_id,
                models,  # jsonb codec encodes the list of {id, modality} dicts
            )
        return row is not None

    async def delete_connection(self, connection_id: str) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM model_connections WHERE id = $1 RETURNING id", connection_id
            )
        return row is not None

    async def set_role_binding(self, role: str, connection_id: str, model: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO role_bindings (role, connection_id, model) VALUES ($1, $2, $3) "
                "ON CONFLICT (role) DO UPDATE SET connection_id = EXCLUDED.connection_id, "
                "model = EXCLUDED.model, updated_at = now()",
                role,
                connection_id,
                model,
            )

    async def list_role_bindings(self) -> list[RoleBinding]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, connection_id, model FROM role_bindings ORDER BY role"
            )
        return [
            RoleBinding(role=r["role"], connection_id=r["connection_id"], model=r["model"])
            for r in rows
        ]

    async def delete_role_binding(self, role: str) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM role_bindings WHERE role = $1 RETURNING role", role
            )
        return row is not None

    async def query_across(
        self, branch_ids: list[str], sections: list[str]
    ) -> dict[str, dict[str, list[dict[str, Any]]]]:
        """Read `sections` (proj_* projection names) across many branches at once (docs/18 B5): the
        core UI of a branching consumer (Hollowloop's loop tree) needed N-times-round-trips before;
        this is ONE query per section (`branch_id = ANY(...)`). Returns {branch_id: {section:
        [rows]}}, rows deterministically ordered. Unknown sections raise (typo-loud)."""
        for s in sections:
            if s not in _SNAPSHOT_TABLES:
                raise ValueError(f"unknown section {s!r}; known: {sorted(_SNAPSHOT_TABLES)}")
        out: dict[str, dict[str, list[dict[str, Any]]]] = {
            b: {s: [] for s in sections} for b in branch_ids
        }
        async with self.pool.acquire() as conn:
            for section in sections:
                cols = _SNAPSHOT_TABLES[section]
                rows = await conn.fetch(
                    f"SELECT branch_id, {', '.join(cols)} FROM proj_{section} "
                    f"WHERE branch_id = ANY($1::text[]) ORDER BY branch_id, {', '.join(cols)}",
                    branch_ids,
                )
                for r in rows:
                    d = dict(r)
                    out[d.pop("branch_id")][section].append(d)
        return out

    async def diff_branches(
        self, branch_a: str, branch_b: str, sections: list[str] | None = None
    ) -> dict[str, dict[str, list[dict[str, Any]]]]:
        """What differs between two branches, per section (docs/18 B5): {section: {added, removed,
        changed}} keyed by each section's PK — `added` = in B not A, `removed` = in A not B,
        `changed` = same PK, different non-key columns (each entry carries the B-side row). Sections
        default to the full projection catalog. Read-only; touches no events/replay."""
        sections = sections or list(_SNAPSHOT_TABLES)
        both = await self.query_across([branch_a, branch_b], sections)
        result: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for section in sections:
            key_cols = _SECTION_KEYS[section]

            def _key(row: dict[str, Any]) -> tuple[Any, ...]:
                return tuple(row[c] for c in key_cols)  # noqa: B023 (key_cols is per-iteration)

            a_by = {_key(r): r for r in both[branch_a][section]}
            b_by = {_key(r): r for r in both[branch_b][section]}
            added = [b_by[k] for k in b_by if k not in a_by]
            removed = [a_by[k] for k in a_by if k not in b_by]
            changed = [b_by[k] for k in b_by if k in a_by and b_by[k] != a_by[k]]
            if added or removed or changed:
                result[section] = {"added": added, "removed": removed, "changed": changed}
        return result

    async def edges_from(self, branch_id: str, src: str) -> list[EdgeView]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT src, rel_type, dst, weight FROM proj_edges "
                "WHERE branch_id = $1 AND src = $2 ORDER BY rel_type, dst",
                branch_id,
                src,
            )
        return [EdgeView(**dict(r)) for r in rows]

    async def world_style(self, branch_id: str) -> tuple[str, dict[str, str]]:
        """The narrator style (tone, joined) + prompt-template overrides for a branch's world
        (docs/09), read from its WorldGenesis. ('', {}) for a world created without a pack."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT e.payload FROM events e "
                "JOIN commits c ON c.commit_id = e.commit_id "
                "JOIN branches b ON b.world_id = c.world_id "
                "WHERE b.branch_id = $1 AND e.event_type = 'WorldGenesis' LIMIT 1",
                branch_id,
            )
        if row is None:
            return "", {}
        payload = row["payload"]
        return ", ".join(payload.get("tone", [])), payload.get("prompt_overrides", {})

    async def world_rule_pack(self, branch_id: str) -> dict[str, Any]:
        """The branch's world's Reaction-Layer rule pack (docs/17), read inline from WorldGenesis.
        {} for a world with no rule pack."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT e.payload FROM events e "
                "JOIN commits c ON c.commit_id = e.commit_id "
                "JOIN branches b ON b.world_id = c.world_id "
                "WHERE b.branch_id = $1 AND e.event_type = 'WorldGenesis' LIMIT 1",
                branch_id,
            )
        if row is None:
            return {}
        return row["payload"].get("rule_pack", {}) or {}

    async def world_ruleset(self, branch_id: str) -> tuple[str, str]:
        """The (ruleset_id, ruleset_version) a branch's world declares, read from its WorldGenesis
        (docs/06). ('', '') for a world created without a pack → the registry default (uro-basic).
        A campaign started on the branch binds this, so the pack's ruleset flows to play."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT e.payload FROM events e "
                "JOIN commits c ON c.commit_id = e.commit_id "
                "JOIN branches b ON b.world_id = c.world_id "
                "WHERE b.branch_id = $1 AND e.event_type = 'WorldGenesis' LIMIT 1",
                branch_id,
            )
        if row is None:
            return "", ""
        payload = row["payload"]
        return payload.get("ruleset_id", ""), payload.get("ruleset_version", "")

    async def items_owned_by(self, branch_id: str, owner_ref: str) -> list[str]:
        """Item ids an actor owns (docs/02) — used to loot a defeated combatant."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT item_id FROM proj_items WHERE branch_id = $1 AND owner_ref = $2 "
                "ORDER BY item_id",
                branch_id,
                owner_ref,
            )
        return [r["item_id"] for r in rows]

    async def get_item(self, branch_id: str, item_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT item_id, name, kind, owner_ref FROM proj_items "
                "WHERE branch_id = $1 AND item_id = $2",
                branch_id,
                item_id,
            )
        return dict(row) if row else None

    async def get_sheet(self, branch_id: str, actor_id: str) -> dict[str, Any] | None:
        """An actor's character sheet as a raw dict (docs/06, D-30). The store keeps it fully
        OPAQUE — the port fixes NO sheet shape; each ruleset owns both shape and semantics and
        validates the dict internally (uro_basic: abilities/hp/ac; uro_pbta: stats + a harm clock,
        no hp). Callers must not assume any field across rulesets."""
        async with self.pool.acquire() as conn:
            sheet = await conn.fetchval(
                "SELECT sheet FROM proj_sheets WHERE branch_id = $1 AND actor_id = $2",
                branch_id,
                actor_id,
            )
        return dict(sheet) if sheet is not None else None

    # --- branching internals ---

    @staticmethod
    async def _resolve_ref(conn: asyncpg.Connection, world_id: str, ref: str) -> str:
        marker = await conn.fetchval(
            "SELECT commit_id FROM markers WHERE world_id = $1 AND name = $2", world_id, ref
        )
        if marker is not None:
            return str(marker)
        commit = await conn.fetchval(
            "SELECT commit_id FROM commits WHERE commit_id = $1 AND world_id = $2", ref, world_id
        )
        if commit is not None:
            return str(commit)
        raise KeyError(f"no marker or commit {ref!r} in world {world_id!r}")

    @staticmethod
    async def _ancestry(conn: asyncpg.Connection, commit_id: str) -> list[asyncpg.Record]:
        """The commit and all its ancestors, genesis-first (ordered by depth)."""
        rows: list[asyncpg.Record] = await conn.fetch(
            """
            WITH RECURSIVE chain AS (
                SELECT commit_id, parent_id, depth FROM commits WHERE commit_id = $1
                UNION ALL
                SELECT c.commit_id, c.parent_id, c.depth
                FROM commits c JOIN chain ON c.commit_id = chain.parent_id
            )
            SELECT commit_id, depth FROM chain ORDER BY depth ASC
            """,
            commit_id,
        )
        return rows

    async def _materialize_into(
        self, conn: asyncpg.Connection, target_branch: str, ancestry: list[asyncpg.Record]
    ) -> int:
        """Build `target_branch`'s projection rows for state at the tip of `ancestry`:
        restore the nearest ancestor snapshot, then replay events after it. Returns the
        number of commits replayed (the replay window) — snapshots keep this bounded, so
        forking is O(window), not O(history). Caller must have created the empty branch."""
        ancestry_ids = [c["commit_id"] for c in ancestry]
        snap = await conn.fetchrow(
            "SELECT s.commit_id, s.state, c.depth "
            "FROM snapshots s JOIN commits c ON c.commit_id = s.commit_id "
            "WHERE s.commit_id = ANY($1::text[]) ORDER BY c.depth DESC LIMIT 1",
            ancestry_ids,
        )
        if snap is not None:
            await restore_snapshot(conn, target_branch, snap["state"])
            from_depth = snap["depth"]
        else:
            from_depth = -1
        replay_ids = [c["commit_id"] for c in ancestry if c["depth"] > from_depth]
        if replay_ids:
            rows = await conn.fetch(
                "SELECT e.event_type, e.payload FROM events e "
                "JOIN commits c ON c.commit_id = e.commit_id "
                "WHERE e.commit_id = ANY($1::text[]) ORDER BY c.depth ASC, e.seq ASC",
                replay_ids,
            )
            for r in rows:
                await apply_raw(conn, target_branch, r["event_type"], r["payload"])
        return len(replay_ids)

    async def _copy_memory(
        self, conn: asyncpg.Connection, target_branch: str, ancestry_ids: list[str]
    ) -> None:
        """Copy memory-index membership rows for the fork's ancestry to the new branch.
        Embeddings themselves live once (by content hash) and are never recomputed — only
        the lightweight pointer rows are duplicated (docs/07). DISTINCT dedups when the
        same commit's memory already exists on more than one source branch (fork of fork)."""
        rows = await conn.fetch(
            "SELECT DISTINCT ON (commit_id, content_hash) "
            "  commit_id, content_hash, kind, text, entity_refs "
            "FROM memory_index WHERE commit_id = ANY($1::text[]) "
            "ORDER BY commit_id, content_hash",
            ancestry_ids,
        )
        for r in rows:
            await conn.execute(
                "INSERT INTO memory_index "
                "(memory_id, branch_id, content_hash, kind, text, entity_refs, commit_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                new_id(),
                target_branch,
                r["content_hash"],
                r["kind"],
                r["text"],
                r["entity_refs"],
                r["commit_id"],
            )

    async def _write_snapshot(
        self, conn: asyncpg.Connection, commit_id: str, branch_id: str
    ) -> None:
        """Capture `branch_id`'s current projection rows as the snapshot for `commit_id`.
        Only sound when `commit_id` is that branch's head (its rows == state there)."""
        blob = await snapshot_state(conn, branch_id)
        state_hash = hashlib.sha256(
            json.dumps(blob, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        await conn.execute(
            "INSERT INTO snapshots (commit_id, state_hash, state) VALUES ($1, $2, $3) "
            "ON CONFLICT (commit_id) DO NOTHING",
            commit_id,
            state_hash,
            blob,
        )

    # --- metering (docs/07, D-14). Operational, prunable; not world truth. ---
    # Note: on EventStore for Phase 0's single store; splits into a UsageRecorder
    # port when the server lands (Phase 5).

    async def record_llm_call(self, call: LLMCall) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO llm_calls "
                "(call_id, stage_tag, model, prompt_hash, tokens_in, tokens_out, latency_ms) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                call.call_id,
                call.stage_tag,
                call.model,
                call.prompt_hash,
                call.tokens_in,
                call.tokens_out,
                call.latency_ms,
            )

    async def usage_by_stage(self, stage: str | None = None) -> list[dict[str, Any]]:
        """Aggregate `llm_calls` by (stage_tag, model): count, token sums, avg latency (docs/07,
        D-14). `stage` scopes to one engine role (uses the `llm_calls_stage_idx`). `model` may be
        NULL (the stub records none) — it groups as its own bucket. Read-only observability."""
        where = "WHERE stage_tag = $1" if stage else ""
        args: list[Any] = [stage] if stage else []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT stage_tag, model, count(*) AS calls, "
                "  coalesce(sum(tokens_in), 0) AS tokens_in, "
                "  coalesce(sum(tokens_out), 0) AS tokens_out, "
                "  round(avg(latency_ms))::int AS avg_latency_ms "
                f"FROM llm_calls {where} "
                "GROUP BY stage_tag, model ORDER BY stage_tag, model NULLS FIRST",
                *args,
            )
            return [dict(r) for r in rows]

    # --- projection queries (ProjectionQueries port; docs/02, 07) ---

    async def get_actor(self, branch_id: str, actor_id: str) -> ActorView | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT actor_id, name, tier, role, aliases, status FROM proj_actors "
                "WHERE branch_id = $1 AND actor_id = $2",
                branch_id,
                actor_id,
            )
        return ActorView(**dict(row)) if row else None

    async def find_actor_by_name(self, branch_id: str, name: str) -> ActorView | None:
        # Match on the CANONICAL form (lower, collapsed whitespace, leading article stripped) so
        # "woman" resolves to a prior "the woman" (mirrors extraction.canonical_name). An exact
        # case-insensitive name still wins the tiebreak over a canonical-only or alias match.
        def canon(col: str) -> str:
            # mirror extraction.canonical_name: lower → collapse+trim whitespace → strip article
            collapsed = f"btrim(regexp_replace(lower({col}), '\\s+', ' ', 'g'))"
            return f"regexp_replace({collapsed}, '^(the|a|an)\\s+', '')"

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT actor_id, name, tier, role, aliases, status FROM proj_actors "
                f"WHERE branch_id = $1 AND ({canon('name')} = {canon('$2')} OR {canon('$2')} = "
                f"ANY(SELECT {canon('a')} FROM unnest(aliases) AS a)) "
                "ORDER BY (lower(name) = lower($2)) DESC, tier DESC, actor_id ASC LIMIT 1",
                branch_id,
                name,
            )
        return ActorView(**dict(row)) if row else None

    async def list_actors(self, branch_id: str) -> list[ActorView]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT actor_id, name, tier, role, aliases, status FROM proj_actors "
                "WHERE branch_id = $1 ORDER BY tier DESC, name ASC",
                branch_id,
            )
        return [ActorView(**dict(r)) for r in rows]

    async def get_claim(self, branch_id: str, claim_id: str) -> ClaimView | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT claim_id, statement, subject_refs, truth, origin, created_day "
                "FROM proj_claims "
                "WHERE branch_id = $1 AND claim_id = $2",
                branch_id,
                claim_id,
            )
        return ClaimView(**dict(row)) if row else None

    async def list_claims(self, branch_id: str) -> list[ClaimView]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT claim_id, statement, subject_refs, truth, origin, created_day "
                "FROM proj_claims "
                "WHERE branch_id = $1 ORDER BY claim_id",
                branch_id,
            )
        return [ClaimView(**dict(r)) for r in rows]

    async def claims_about(self, branch_id: str, entity_ref: str) -> list[ClaimView]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT claim_id, statement, subject_refs, truth, origin, created_day "
                "FROM proj_claims "
                "WHERE branch_id = $1 AND subject_refs @> ARRAY[$2]::text[] "
                "ORDER BY claim_id",  # deterministic — recall/replay must be reproducible
                branch_id,
                entity_ref,
            )
        return [ClaimView(**dict(r)) for r in rows]

    async def beliefs_of(self, branch_id: str, actor_id: str) -> list[BeliefView]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT actor_id, claim_id, confidence, learned_from FROM proj_beliefs "
                "WHERE branch_id = $1 AND actor_id = $2 "
                "ORDER BY claim_id",  # deterministic — recall/replay must be reproducible
                branch_id,
                actor_id,
            )
        return [BeliefView(**dict(r)) for r in rows]

    async def fact_consistency(self, branch_id: str) -> tuple[int, int]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT count(*) FILTER (WHERE truth = 'true') AS consistent, count(*) AS total "
                "FROM proj_claims WHERE branch_id = $1 AND origin = 'narrator'",
                branch_id,
            )
        return (row["consistent"], row["total"])

    # --- semantic memory (VectorIndex port; docs/04, 07) ---

    async def add_memory(
        self,
        *,
        branch_id: str,
        commit_id: str,
        kind: str,
        text: str,
        vector: list[float],
        entity_refs: list[str],
    ) -> None:
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO embeddings (content_hash, vector) VALUES ($1, $2::vector) "
                "ON CONFLICT (content_hash) DO NOTHING",  # a vector lives once
                content_hash,
                _vector_literal(vector),
            )
            await conn.execute(
                "INSERT INTO memory_index "
                "(memory_id, branch_id, content_hash, kind, text, entity_refs, commit_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                new_id(),
                branch_id,
                content_hash,
                kind,
                text,
                entity_refs,
                commit_id,
            )

    async def search(self, branch_id: str, vector: list[float], k: int) -> list[MemoryHit]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT m.text, m.kind, m.commit_id, m.entity_refs, "
                "       (e.vector <=> $2::vector) AS distance "
                "FROM memory_index m JOIN embeddings e ON e.content_hash = m.content_hash "
                "WHERE m.branch_id = $1 "
                "ORDER BY distance ASC, m.memory_id ASC LIMIT $3",  # memory_id: stable tiebreak
                branch_id,
                _vector_literal(vector),
                k,
            )
        return [MemoryHit(**dict(r)) for r in rows]

    # --- helpers ---

    @staticmethod
    async def _insert_events(
        conn: asyncpg.Connection, commit_id: str, events: list[DomainEvent]
    ) -> None:
        for seq, event in enumerate(events):
            await conn.execute(
                "INSERT INTO events (event_id, commit_id, seq, event_type, "
                "entity_refs, world_time, caused_by, payload) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                event.event_id,
                commit_id,
                seq,
                event.event_type,
                event.entity_refs,
                event.world_time.model_dump(),
                event.caused_by.model_dump(by_alias=True),  # history `pass` on the wire (docs/12)
                event.payload,
            )
