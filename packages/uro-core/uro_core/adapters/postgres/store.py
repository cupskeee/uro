"""PostgreSQL EventStore adapter (docs/07, D-13, D-18).

asyncpg + hand-written SQL, no ORM. Implements the EventStore port. Lives in the
adapters ring — the core never imports this module (import-linter, docs/14).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import asyncpg

from uro_core.adapters.postgres.projector import apply_event
from uro_core.domain.events import BeatResolvedPayload, DomainEvent, world_genesis
from uro_core.domain.hashing import compute_commit_hash
from uro_core.domain.ids import new_id
from uro_core.metering import LLMCall
from uro_core.timeline.models import (
    ActorView,
    BeliefView,
    Campaign,
    ClaimView,
    Commit,
    MemoryHit,
    World,
)


def _vector_literal(vector: list[float]) -> str:
    """pgvector text form: '[0.1,0.2,...]' — passed as text and cast ::vector in SQL."""
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"


_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def _init_conn(conn: asyncpg.Connection) -> None:
    # asyncpg does not encode dict <-> jsonb automatically; register a codec.
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


class PostgresEventStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

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
            if parent_id is not None:
                parent_hash = await conn.fetchval(
                    "SELECT commit_hash FROM commits WHERE commit_id = $1", parent_id
                )
            commit_id = new_id()
            commit_hash = compute_commit_hash(parent_hash, events)
            await conn.execute(
                "INSERT INTO commits (commit_id, world_id, parent_id, commit_hash) "
                "VALUES ($1, $2, $3, $4)",
                commit_id,
                branch["world_id"],
                parent_id,
                commit_hash,
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
            return Commit(
                commit_id=commit_id,
                world_id=branch["world_id"],
                parent_id=parent_id,
                commit_hash=commit_hash,
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
