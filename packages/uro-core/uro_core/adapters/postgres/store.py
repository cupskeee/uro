"""PostgreSQL EventStore adapter (docs/07, D-13, D-18).

asyncpg + hand-written SQL, no ORM. Implements the EventStore port. Lives in the
adapters ring — the core never imports this module (import-linter, docs/14).
"""

from __future__ import annotations

import json
from pathlib import Path

import asyncpg

from uro_core.domain.events import BeatResolvedPayload, DomainEvent, world_genesis
from uro_core.domain.hashing import compute_commit_hash
from uro_core.domain.ids import new_id
from uro_core.timeline.models import Campaign, Commit, World

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
                ORDER BY chain.depth ASC, e.seq ASC
                LIMIT $2
                """,
                branch_id,
                limit,
            )
        # rows are newest-first; present oldest-first for chat reconstruction.
        return [BeatResolvedPayload(**r["payload"]) for r in reversed(rows)]

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
