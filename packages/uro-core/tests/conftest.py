import os
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio
from uro_core.adapters.postgres.store import PostgresEventStore

DSN = os.environ.get("URO_DATABASE_URL", "postgresql://uro:uro@localhost:5433/uro")


@pytest_asyncio.fixture
async def store() -> AsyncIterator[PostgresEventStore]:
    """A connected, migrated Postgres store — skips the test if no DB is reachable."""
    s = PostgresEventStore(DSN)
    try:
        await s.connect()
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - env dependent
        pytest.skip(f"Postgres unavailable at {DSN} ({exc})")
    try:
        await s.migrate()
        yield s
    finally:
        await s.close()
