# Contributing to Uro Engine

Thanks for your interest. Uro is a proof-of-concept engine, but it's built to real
standards — the same gate runs in CI and locally, and the architecture is enforced, not
just documented.

## Setup

Prerequisites: **Python 3.12+**, [`uv`](https://docs.astral.sh/uv/), **Docker**, and
[`just`](https://github.com/casey/just) (optional but recommended).

```sh
uv sync --all-packages          # install the workspace
docker compose up -d --wait     # Postgres 17 + pgvector on host port 5433
uv run uro db migrate           # apply the forward-only SQL migrations
just test                       # the full gate (see below)
```

The full developer guide — tooling, migrations, testing strategy, and the definition of
done — is [docs/14-development-guide.md](docs/14-development-guide.md). The working
conventions and the build rhythm this repo follows are in [CLAUDE.md](CLAUDE.md).

## The gate

`just test` must be green before a change is done. It runs:

- `ruff check` + `ruff format --check` (line length 100)
- `mypy` — **strict on the core ring** (`domain` / `timeline` / `pipeline`), lenient on adapters
- `import-linter` — enforces the hexagonal boundary (below)
- `pytest` — deterministic, **no live LLM calls** (provider paths use recorded/mock transports
  or the deterministic stub). Postgres must be up; DB-requiring tests auto-skip if it's down.

## Project invariants (please don't break these)

These are load-bearing; several are enforced mechanically:

- **Hexagonal boundary (CI-enforced by import-linter).** The core ring imports only *ports*,
  never a concrete adapter (`adapters/`, `providers/adapters/`, a concrete ruleset).
- **Everything is an event.** State changes are typed domain events; projections are
  rebuildable read-models written *only* by the projector, in the same transaction as the
  commit. Never mutate a projection directly.
- **Migrations are forward-only** (numbered SQL in `adapters/postgres/migrations/`). A bad one
  gets a new correcting migration, never an edit.
- **Tests assert on committed events, not prose.** Structured LLM output is schema-validated;
  the extractor is fenced by a schema + an emitter whitelist so it cannot mint mechanical,
  lethal, or protected-canon events.
- **The docs are authoritative.** When code and docs disagree, fix one to match the other in
  the *same* commit. Design lives in [`docs/`](docs/); decisions append to
  [`docs/decisions.md`](docs/decisions.md) (never edit a past decision — a reversal is a new
  entry pointing at the old one).

## Submitting changes

`main` is the always-releasable trunk — **don't develop directly on it.**

1. Branch from `main` (`feat/…`, `fix/…`, `docs/…`).
2. Make the change; keep it a coherent slice, and reconcile any doc/code drift in the same commit.
3. Add a note under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) if the change is user-visible.
4. Run `just test` — green.
5. Open a pull request describing *what* and *why*. CI runs the same gate on every push and PR;
   merge to `main` only when it's green (self-merge is fine for a solo project).

## Versioning & releases

Uro uses [Semantic Versioning](https://semver.org/), single-versioned across the workspace (all
three packages share one number; pre-1.0, a MINOR bump is a notable/breaking change and a PATCH is
a fix). Changes accumulate under `## [Unreleased]` in the CHANGELOG.

To cut a release: bump the version in the three `packages/*/pyproject.toml`, move the CHANGELOG
`[Unreleased]` entries under a new `## [X.Y.Z]` section, then `just release X.Y.Z` (it gates + tags)
and `git push origin main --follow-tags`. Pushing the tag triggers the release workflow, which
creates the GitHub Release from that version's CHANGELOG notes. Never `push --force` `main`. The
full process is in [docs/14-development-guide.md](docs/14-development-guide.md#versioning--releases).

Bug reports and feature ideas are welcome via [issues](https://github.com/cupskeee/uro/issues).
For anything security-sensitive, see [SECURITY.md](SECURITY.md) instead of opening a public issue.
