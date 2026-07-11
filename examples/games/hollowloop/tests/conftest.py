"""Pytest wiring for HOLLOWLOOP. The repo's root pytest config collects only `packages/`
(pyproject.toml testpaths), so this suite is run EXPLICITLY:

    uv run pytest examples/games/hollowloop/tests

Postgres must be up (docker compose up -d --wait); tests skip cleanly when it is not — the same
convention as packages/uro-core/tests/conftest.py and the sibling games.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

GAME_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GAME_DIR))

from uro_core.adapters.postgres.store import PostgresEventStore  # noqa: E402

DSN = os.environ.get("URO_DATABASE_URL", "postgresql://uro:uro@localhost:5433/uro")


@pytest_asyncio.fixture
async def store() -> PostgresEventStore:
    import asyncpg

    s = PostgresEventStore(DSN)
    try:
        await s.connect()
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover — env-dependent
        pytest.skip(f"Postgres unavailable at {DSN} ({exc})")
    try:
        await s.migrate()
        yield s
    finally:
        await s.close()
