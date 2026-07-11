"""IRONWAKE test fixtures. Run from the repo root:

    uv run pytest examples/games/ironwake/tests

DB tests skip when Postgres is down (same convention as uro-core's suite). The `ironwake`
package is imported the way a consumer's project would — its parent folder on the path."""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # examples/games

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
