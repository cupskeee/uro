# 14 — Development Guide

Everything needed to go from `git clone` to a running engine and a green test suite. Tooling decisions recorded as D-18.

## Prerequisites

Python **3.12+** · [`uv`](https://docs.astral.sh/uv/) · Docker (for Postgres) · `just` (task runner; optional, targets also runnable by hand).

## Repo layout & workspace

`uv` workspace monorepo (layout per `01-architecture.md`):

```toml
# ./pyproject.toml
[tool.uv.workspace]
members = ["packages/uro-core", "packages/uro-server", "packages/uro-cli"]
```

`uro-server` and `uro-cli` depend on `uro-core` as workspace source deps. Dev deps (shared, root): `pytest`, `pytest-asyncio`, `ruff`, `mypy`, `import-linter`, `coverage`.

Core runtime deps (uro-core): `pydantic`, `asyncpg`, `pgvector`, `python-ulid`, `jinja2`, `httpx`. Server adds `fastapi`, `uvicorn`; CLI adds `typer`, `rich`.

## Local infrastructure

```yaml
# docker-compose.yml
services:
  postgres:
    image: pgvector/pgvector:pg17
    environment: { POSTGRES_DB: uro, POSTGRES_USER: uro, POSTGRES_PASSWORD: uro }
    ports: ["5433:5432"]   # host 5433 — leaves the default port free for other local Postgres instances
    volumes: [pgdata:/var/lib/postgresql/data]
```

No SQLite mode (D-13): local *is* Postgres, identical to live.

## Database access & migrations

- **asyncpg + hand-written SQL, no ORM** (D-18). The queries are simple (append events, load snapshots+replay, projection upserts, recursive CTEs) and the DDL in `07` is the real interface.
- Migrations: numbered SQL files in `packages/uro-core/uro_core/adapters/postgres/migrations/NNN_name.sql`, applied in order by `uro db migrate`, tracked in a `schema_migrations` table. Forward-only; a bad migration gets a new correcting migration, never an edit.
- Projections are **not** migrated when their shape changes — they're rebuilt from the log (versioned by projector code hash, `07`).

## Conventions

- **Import discipline enforced, not hoped for:** an `import-linter` contract forbids `uro_core.{domain,timeline,engines,pipeline,memory}` from importing `fastapi|asyncpg|httpx|uro_core.adapters|uro_core.providers.adapters|uro_core.rulesets.uro_basic` — the hexagonal rule from `01` as a CI failure. The last two targets are load-bearing (D-27): the core ring may import the provider/ruleset **ports** (`uro_core.providers`, `uro_core.rulesets`) but never the concrete impls, and a leaf-library ban alone misses them — the pure-Python ruleset built-in pulls in no bannable library at all.
- IDs: ULID everywhere (`python-ulid`). Wall-clock timestamps UTC; never conflate with `world_time`.
- `ruff` for lint + format (line length 100); `mypy --strict` on `domain/`, `timeline/`, `pipeline/`; lenient on adapters.
- Async end-to-end; no blocking I/O outside adapters.
- Docs discipline (from `10`): behavior and its doc change in the same commit; new decisions append to `decisions.md`; settled OQs move out of `11`.

## Configuration

Precedence (highest wins): **env vars (`URO_*`) → deployment `uro.toml` → world-pack suggestions → engine defaults.**

```toml
# uro.toml (deployment config — NEVER committed with secrets; keys come from env/keyring)
[database]  url = "postgresql://uro:uro@localhost:5433/uro"
[server]    mode = "local"          # local | token (08)
[llm.providers.anthropic]  api_key_env = "ANTHROPIC_API_KEY"
[llm.providers.local]      base_url = "http://localhost:11434/v1"
[llm.roles]                # deployment bindings override world-pack suggestions (04)
narrator = "anthropic:claude-sonnet-5"
extractor = "local:qwen3-14b"
```

Secrets resolve via env or OS keychain (`04`); `uro.toml` holds *references*, exports never include either.

## Testing strategy

The pyramid, and what "assert on events, not prose" means in practice:

1. **Domain/unit** — pure logic (timeline math, promotion rules, validation gauntlet, Uro Basic) with zero I/O. Fast, exhaustive.
2. **Stage tests with recorded providers** — a `RecordingProvider` adapter wraps a real one: first run records `(prompt_hash → response)` to JSON fixtures in `tests/fixtures/llm/`; CI replays only. Stage tests feed a `BeatState`, replay the LLM, and assert on the *structured* output (BeatPlan fields, ProposedEvents) — never on prose strings.
3. **Pipeline/integration** — full beat against Dockerized Postgres, recorded providers. Assert on **committed events** (types, payloads, refs) and projections.
4. **Acceptance per phase** — each roadmap phase's acceptance test (`10`) automated as far as nondeterminism allows: the meteor test, for example, asserts branch topology, carried/dropped state, and chronicle content — not the wording of the narration.
5. **Live smoke (manual, not CI)** — `just play` against a real model before calling a phase done. **CI never makes live LLM calls.**

Re-recording fixtures is a deliberate act (`just record`), reviewed like a snapshot-test update.

## Task runner

```
just up        # docker compose up -d + migrate
just migrate   # uro db migrate
just test      # ruff check + mypy + import-linter + pytest (replay mode)
just record    # re-record LLM fixtures against live providers (local decision, reviewed)
just play      # uro play against the demo world (live smoke)
```

## CI

GitHub Actions (or any runner): lint → types → import-linter → unit/stage/integration with Postgres service container, replay-only. Merge = green. That's the whole gate for a solo project; don't build more ceremony than that.

## Definition of done (any phase)

The phase's acceptance test passes automated in CI (replay) **and** once live (manual smoke); docs updated; decisions/OQs updated. Then — and only then — the next phase starts.
