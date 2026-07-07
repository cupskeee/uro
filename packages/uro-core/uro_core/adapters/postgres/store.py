"""PostgreSQL EventStore adapter (docs/07, D-13, D-18).

asyncpg + hand-written SQL, no ORM. Implements the EventStore port. Lives in the
adapters ring — the core never imports this module (import-linter, docs/14).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import asyncpg

from uro_core.adapters.postgres.projector import (
    apply_event,
    apply_raw,
    restore_snapshot,
    snapshot_state,
)
from uro_core.domain.events import BeatResolvedPayload, DomainEvent, world_genesis
from uro_core.domain.hashing import compute_commit_hash
from uro_core.domain.ids import new_id
from uro_core.metering import LLMCall
from uro_core.timeline.models import (
    ActorView,
    BeliefView,
    Branch,
    BranchInfo,
    Campaign,
    ClaimView,
    Commit,
    LineageEntry,
    Marker,
    MemoryHit,
    PlaceView,
    World,
)

# Full projection state is serialized every this-many commits (docs/03: N≈50) and
# always at markers. Materialization restores the nearest ancestor snapshot then
# replays forward, so this only trades snapshot storage for replay length — never
# correctness. Instance-overridable so tests can exercise the snapshot path cheaply.
SNAPSHOT_EVERY = 50


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

    async def create_world(self, name: str) -> World:
        async with self.pool.acquire() as conn, conn.transaction():
            world_id = new_id()
            await conn.execute(
                "INSERT INTO worlds (world_id, name) VALUES ($1, $2)", world_id, name
            )
            genesis = world_genesis(name)
            commit_id = new_id()
            commit_hash = compute_commit_hash(None, [genesis])
            await conn.execute(
                "INSERT INTO commits (commit_id, world_id, parent_id, commit_hash) "
                "VALUES ($1, $2, NULL, $3)",
                commit_id,
                world_id,
                commit_hash,
            )
            await self._insert_events(conn, commit_id, [genesis])
            branch_id = new_id()
            await conn.execute(
                "INSERT INTO branches (branch_id, world_id, name, head_commit) "
                "VALUES ($1, $2, 'main', $3)",
                branch_id,
                world_id,
                commit_id,
            )
            return World(world_id=world_id, name=name, main_branch_id=branch_id)

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

    async def create_campaign(self, world_id: str, branch_id: str) -> Campaign:
        campaign_id = new_id()
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO campaigns (campaign_id, world_id, branch_id) VALUES ($1, $2, $3)",
                campaign_id,
                world_id,
                branch_id,
            )
        return Campaign(campaign_id=campaign_id, world_id=world_id, branch_id=branch_id)

    async def get_campaign(self, campaign_id: str) -> Campaign | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT campaign_id, world_id, branch_id FROM campaigns WHERE campaign_id = $1",
                campaign_id,
            )
        return Campaign(**dict(row)) if row else None

    # --- the timeline ---

    async def append_beat(self, branch_id: str, events: list[DomainEvent]) -> Commit:
        async with self.pool.acquire() as conn, conn.transaction():
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

    # --- projection queries (ProjectionQueries port; docs/02, 07) ---

    async def get_actor(self, branch_id: str, actor_id: str) -> ActorView | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT actor_id, name, tier, role, aliases FROM proj_actors "
                "WHERE branch_id = $1 AND actor_id = $2",
                branch_id,
                actor_id,
            )
        return ActorView(**dict(row)) if row else None

    async def find_actor_by_name(self, branch_id: str, name: str) -> ActorView | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT actor_id, name, tier, role, aliases FROM proj_actors "
                "WHERE branch_id = $1 AND (lower(name) = lower($2) "
                "  OR lower($2) = ANY(SELECT lower(a) FROM unnest(aliases) AS a)) "
                # exact name beats an alias-only match; then tier; actor_id is a stable tiebreak.
                "ORDER BY (lower(name) = lower($2)) DESC, tier DESC, actor_id ASC LIMIT 1",
                branch_id,
                name,
            )
        return ActorView(**dict(row)) if row else None

    async def list_actors(self, branch_id: str) -> list[ActorView]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT actor_id, name, tier, role, aliases FROM proj_actors "
                "WHERE branch_id = $1 ORDER BY tier DESC, name ASC",
                branch_id,
            )
        return [ActorView(**dict(r)) for r in rows]

    async def get_claim(self, branch_id: str, claim_id: str) -> ClaimView | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT claim_id, statement, subject_refs, truth, origin FROM proj_claims "
                "WHERE branch_id = $1 AND claim_id = $2",
                branch_id,
                claim_id,
            )
        return ClaimView(**dict(row)) if row else None

    async def list_claims(self, branch_id: str) -> list[ClaimView]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT claim_id, statement, subject_refs, truth, origin FROM proj_claims "
                "WHERE branch_id = $1 ORDER BY claim_id",
                branch_id,
            )
        return [ClaimView(**dict(r)) for r in rows]

    async def claims_about(self, branch_id: str, entity_ref: str) -> list[ClaimView]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT claim_id, statement, subject_refs, truth, origin FROM proj_claims "
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
                event.caused_by.model_dump(),
                event.payload,
            )
