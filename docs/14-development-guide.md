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

**The test suite runs against a DEDICATED `uro_test` database** (auto-created on the same server),
NEVER the `uro` DB your `uro serve` / uro-loom use — so `just test` can't pollute the worlds you
browse. (The DB tests create worlds/campaigns and don't roll back; keeping them in a throwaway DB is
the isolation. Set `URO_DATABASE_URL` to override — CI points it at its own service container.)
Reset the test DB any time with `dropdb -h localhost -p 5433 -U uro uro_test`.

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

> **Proposed (D-47, [docs/20](20-model-connections.md)):** move LLM provider config into an
> instance-level, DB-backed registry configurable over the `uro-server` API (from `uro-cli` /
> `uro-loom`), demoting `uro.toml [llm.roles]` + `--provider` to a bootstrap seed. Only `[llm.roles]`
> is wired today — the `[database]`/`[server]`/`[llm.providers.*]` sections above are aspirational and
> get reconciled when D-47 lands.

## Testing strategy

The pyramid, and what "assert on events, not prose" means in practice:

1. **Domain/unit** — pure logic (timeline math, promotion rules, validation gauntlet, Uro Basic) with zero I/O. Fast, exhaustive.
2. **Stage tests with recorded providers** — a `RecordingProvider` adapter wraps a real one: first run records `(prompt_hash → response)` to JSON fixtures in `tests/fixtures/llm/`; CI replays only. Stage tests feed a `BeatState`, replay the LLM, and assert on the *structured* output (BeatPlan fields, ProposedEvents) — never on prose strings.
3. **Pipeline/integration** — full beat against Dockerized Postgres, recorded providers. Assert on **committed events** (types, payloads, refs) and projections.
4. **Acceptance per phase** — each roadmap phase's acceptance test (`10`) automated as far as nondeterminism allows: the meteor test, for example, asserts branch topology, carried/dropped state, and chronicle content — not the wording of the narration.
5. **Live smoke (manual, not CI)** — `just play` against a real model before calling a phase done. **CI never makes live LLM calls.**

Re-recording fixtures is a deliberate act (`just record`), reviewed like a snapshot-test update.

## Task runner

```sh
just up        # docker compose up -d + migrate
just migrate   # uro db migrate
just test      # ruff check + mypy + import-linter + pytest (replay mode)
just record    # re-record LLM fixtures against live providers (local decision, reviewed)
just play      # uro play against the demo world (live smoke)
```

## CI

GitHub Actions (or any runner): lint → types → import-linter → unit/stage/integration with Postgres service container, replay-only. Merge = green. That's the whole gate for a solo project; don't build more ceremony than that.

## Versioning & releases

The engine is versioned with **Semantic Versioning**, single-versioned across the workspace (all
three packages share one number). Pre-1.0 (`0.MINOR.PATCH`): a **MINOR** bump is a notable or
breaking change; a **PATCH** is a fix. `1.0.0` is cut when the public API (the `uro_core` surface,
the event catalog, the world-pack + export formats) is declared stable. `CHANGELOG.md` follows
[Keep a Changelog](https://keepachangelog.com): every notable change lands under `## [Unreleased]`.

**Branch discipline (don't develop straight on `main`).** `main` is the always-releasable trunk.

- Do work on a short-lived branch (`feat/…`, `fix/…`, `docs/…`) and open a **pull request**; merge
  to `main` only when the gate is green. Self-review + self-merge is fine for a solo project — the
  point is that `main` only ever moves through a green, reviewable step, not that a second human signs off.
- Keep each PR a coherent slice; reconcile any doc/code drift in the same PR.
- Never `push --force` `main` once it's public — a history rewrite forces every clone to re-sync and
  breaks anyone building on it. (Pre-public cleanup is the only exception, done once.)
- A change destined for the next release adds a `CHANGELOG.md [Unreleased]` entry as part of the PR.

**Cutting a release.** A release is a tag + GitHub Release notes (via `release.yml`). Publishing the
packages to PyPI is a *separate, owner-activated* step (see below). Steps:

1. On a green `main`, bump the `version` in all three `packages/*/pyproject.toml` to `X.Y.Z`, and
   the internal `uro-core[...]==X.Y.Z` / `uro-server==X.Y.Z` pins in the `uro-cli` and `uro-server`
   deps to match (the workspace is single-versioned; the pins keep a partial `pip install --upgrade`
   from mismatching them). `uv sync` fails loudly if a pin drifts from the workspace version, so a
   forgotten bump is caught by the gate. (`__version__` is read from installed metadata, so it
   follows the pyproject automatically — no separate copy to sync.)
2. In `CHANGELOG.md`, move the `[Unreleased]` entries under a new `## [X.Y.Z] - YYYY-MM-DD` section
   (and add its compare/tag link at the bottom).
3. Commit (`release: vX.Y.Z`), open/merge the PR.
4. `just release X.Y.Z` — it verifies the version + CHANGELOG section, runs the gate, and creates
   the annotated tag `vX.Y.Z`.
5. `git push origin main --follow-tags`. Pushing the tag triggers `.github/workflows/release.yml`,
   which creates the GitHub Release with that version's CHANGELOG section as the notes.

### Publishing to PyPI (owner-activated)

Distribution posture (D-43): `uro-core` is embeddable from git today, and the packages can also be
published to PyPI. Publishing is deliberately **manual and owner-activated** — it needs a one-time
account setup the workflow can't do for you, and there is no downstream consumer forcing a cadence.

**Package layout.** The base `uro-core` install is the pure engine (it imports only ports, no DB
driver or HTTP client); the bundled adapters are extras — `uro-core[postgres]` (the Postgres +
pgvector store), `uro-core[llm]` (the LLM provider adapters), `uro-core[all]` (both). `uro-cli` and
`uro-server` pull both extras. All **three** packages publish together: `uro-cli` imports
`uro-server` (for `uro serve`), which builds on `uro-core`, and they are single-versioned.

**One-time setup (owner).** Each package gets its **own** GitHub environment + trusted publisher.
They cannot share one environment: a PyPI *pending* publisher must be unique on
`(owner, repo, workflow, environment)`, so three packages on `environment: pypi` collide ("a pending
trusted publisher matching this configuration has already been registered for a different project
name"). So `publish.yml` runs one job per package in a distinct environment:

| PyPI project | GitHub environment | Owner | Repository | Workflow |
|---|---|---|---|---|
| `uro-core` | `pypi-core` | `cupskeee` | `uro` | `publish.yml` |
| `uro-server` | `pypi-server` | `cupskeee` | `uro` | `publish.yml` |
| `uro-cli` | `pypi-cli` | `cupskeee` | `uro` | `publish.yml` |

1. In repo **Settings → Environments**, create `pypi-core`, `pypi-server`, `pypi-cli` (empty is fine;
   add required reviewers on any of them for a manual approval gate before that upload).
2. On **pypi.org → Account → Publishing**, add a *pending publisher* per row above (Workflow name is
   the filename `publish.yml`; Environment is the matching `pypi-<pkg>`). Field values must match
   character-for-character.

**Each release.** After cutting the release (steps above), run the publish workflow: Actions →
**publish** → *Run workflow*. Jobs run `uro-core` → `uro-server` → `uro-cli` (a dependency lands on
PyPI before its dependents) and upload via trusted publishing — no API tokens anywhere. Set up all
three publishers before the first run so it completes in one pass. (A `ghcr.io` `uro-server` image is
a further, still-deferred step: it needs a Dockerfile, and `uro-server` is a thin shell for now.)

## Definition of done (any phase)

The phase's acceptance test passes automated in CI (replay) **and** once live (manual smoke); docs updated; decisions/OQs updated. Then — and only then — the next phase starts.
