"""Pytest wiring for The Seventh Vault. NOTE: the repo's root pytest config collects only
`packages/` (pyproject.toml testpaths), so this suite is run EXPLICITLY:

    uv run pytest examples/games/seventh-vault/tests

Postgres must be up (docker compose up -d); tests skip cleanly when it is not — the same
convention as packages/uro-core/tests/conftest.py and the ironwake example.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

GAME_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GAME_DIR))

DSN = os.environ.get("URO_DATABASE_URL", "postgresql://uro:uro@localhost:5433/uro")


@pytest.fixture
async def pg_available() -> None:
    """Skip the test when Postgres is down (CI without the compose stack)."""
    import asyncpg
    from uro_core.adapters.postgres.store import PostgresEventStore

    store = PostgresEventStore(DSN)
    try:
        await store.connect()
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover — env-dependent
        pytest.skip(f"Postgres unavailable at {DSN} ({exc})")
    finally:
        await store.close()
