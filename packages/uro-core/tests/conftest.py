import os
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio
from uro_core.adapters.postgres.store import PostgresEventStore

# Tests run against a DEDICATED `uro_test` database — NOT the `uro` DB the running instance
# (`uro serve` / uro-loom) uses. Otherwise every `just test` run pollutes the worlds you browse
# (the DB tests create worlds/campaigns and don't roll back). An explicit URO_DATABASE_URL (CI, or
# an override) is used as-is — CI's DB is already a throwaway service container.
_EXPLICIT = os.environ.get("URO_DATABASE_URL")
_APP_DSN = "postgresql://uro:uro@localhost:5433/uro"  # only used to CREATE the test DB
DSN = _EXPLICIT or "postgresql://uro:uro@localhost:5433/uro_test"

_ensured = False


async def _ensure_test_db() -> None:
    """Create the dedicated `uro_test` DB if absent (local only; once per session)."""
    global _ensured
    if _ensured or _EXPLICIT:
        return
    admin = await asyncpg.connect(_APP_DSN)
    try:
        if not await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = 'uro_test'"):
            await admin.execute("CREATE DATABASE uro_test")
    finally:
        await admin.close()
    _ensured = True


@pytest_asyncio.fixture
async def store() -> AsyncIterator[PostgresEventStore]:
    """A connected, migrated store on the DEDICATED test DB — skips if no DB is reachable."""
    s = PostgresEventStore(DSN)
    try:
        await _ensure_test_db()
        await s.connect()
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - env dependent
        pytest.skip(f"Postgres unavailable at {DSN} ({exc})")
    try:
        await s.migrate()
        yield s
    finally:
        await s.close()
