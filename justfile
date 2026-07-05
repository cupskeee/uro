# Task runner targets per docs/14-development-guide.md.

default: test

# Start local infrastructure (Postgres + pgvector) and wait for health.
up:
    docker compose up -d --wait

down:
    docker compose down

# Full check suite — CI-equivalent. Replay mode only; never calls live LLMs.
test:
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy
    uv run lint-imports
    uv run pytest

# Auto-fix formatting and lint.
fmt:
    uv run ruff format .
    uv run ruff check --fix .

# --- Arriving with Phase 0 implementation (docs/10) ---
# migrate:  uv run uro db migrate
# play:     uv run uro play <campaign>       (live smoke, not CI)
# record:   re-record LLM fixtures against live providers (reviewed like snapshots)
