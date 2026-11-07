"""`uro serve` refuses a database its code's migrations haven't reached (2026-11-08): code that
writes a column a migration adds otherwise fails at the first write, mid-play. Runs on a throwaway
database, so the shared test database's migration record is never touched."""

from __future__ import annotations

import asyncpg
import pytest
from tests.conftest import DSN
from uro_core.adapters.postgres.store import _MIGRATIONS_DIR, PostgresEventStore
from uro_core.domain.ids import new_id


async def test_pending_migrations_lists_what_the_database_lacks() -> None:
    admin_dsn = DSN.rsplit("/", 1)[0] + "/postgres"
    name = f"uro_mig_{new_id().lower()}"
    try:
        admin = await asyncpg.connect(admin_dsn)
    except (OSError, asyncpg.PostgresError):
        pytest.skip("no database reachable")
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        store = PostgresEventStore(DSN.rsplit("/", 1)[0] + f"/{name}")
        await store.connect()
        try:
            every = [p.stem for p in sorted(_MIGRATIONS_DIR.glob("*.sql"))]
            assert await store.pending_migrations() == every  # never migrated: all pending
            assert await store.migrate() == every
            assert await store.pending_migrations() == []
        finally:
            await store.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await admin.close()
