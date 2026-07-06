# CLAUDE.md — working in this repo

Uro Engine: a headless, game-agnostic AI-RPG **engine** PoC (git-is-to-GitHub as Uro-is-to-a-game/platform). Python 3.12, `uv` workspace. Full design in `docs/` — read `docs/15-walkthroughs.md` for the map, `docs/10-roadmap.md` for status, `docs/decisions.md` (D-1..D-28) for why things are the way they are. **The docs are authoritative; when code and docs disagree, fix one to match the other in the same commit.**

## Layout

`packages/uro-core` (all engine logic, embeddable) · `packages/uro-server` (thin FastAPI shell — scaffold only so far) · `packages/uro-cli` (`uro` reference client). Design detail: `docs/01-architecture.md`.

## Commands

```
uv sync --all-packages              # install the workspace
docker compose up -d --wait         # Postgres + pgvector on HOST PORT 5433 (not 5432)
uv run uro db migrate               # apply forward-only SQL migrations
just test                           # THE gate: ruff + ruff format --check + mypy + import-linter + pytest
just fmt                            # ruff format + --fix
uv run uro world new "Name"         # → a campaign id
uv run uro play <campaign>          # interactive; --provider stub|local|openai|anthropic ; --bare = ablation
uv run uro consistency <campaign>   # thesis proxy metric T2
```

`just test` needs Postgres up; DB-requiring tests auto-skip if it's down. **CI never makes live LLM calls** — provider-dependent tests use recorded/mock transports or the deterministic stub. No credentials or Ollama were available in the build environment, so the live-model run is pending (commands in `docs/10` Thesis section).

## Non-negotiable conventions

- **Hexagonal boundary (enforced in CI by import-linter):** the core ring — `uro_core.{domain,timeline,engines,pipeline,memory}` — imports only **ports**, never concrete adapters (`adapters/`, `providers/adapters/`, `rulesets/uro_basic`). Keep the contract KEPT.
- **Everything is an event.** State changes are typed domain events (`domain/events.py`, catalog in `docs/12`); projections are rebuildable read-models written only by the projector, in the same transaction as the commit. Never mutate a projection directly.
- **Migrations are forward-only** (numbered SQL in `adapters/postgres/migrations/`); a bad one gets a new correcting migration, never an edit.
- **Tests assert on committed events, not prose.** Structured output is validated (Pydantic); the extractor is fenced by schema + gauntlet.
- **mypy is strict on the core ring** (`domain`/`timeline`/`pipeline`); adapters lenient. Line length 100.
- **Commit messages: NO `Co-Authored-By` footer** (owner preference). Shells choke on backticks/`<>` in `-m` — write the message to a scratch file and `git commit -F`.
- **Decisions append to `docs/decisions.md`** (never edit a past decision; a reversal is a new entry pointing at the old). Settled open-questions move out of `docs/11`.

## How increments get built (the rhythm that's worked)

Build one coherent, tightly-coupled slice **directly** (don't fan out — layers interlock). Then run a **multi-dimension adversarial review** via the Workflow tool (4–5 dimensions → find → verify each against the ACTUAL code, default REJECTED). Triage verified verdicts; fix confirmed-real, defer verifier-agreed non-issues (name them in the commit). This has caught a real edge-case bug in every increment. Prior review scripts are under the session workflow dir.

**Watch for overclaiming:** trace whether the code actually enforces a guarantee before writing it; prefer "by construction (structural)" vs "by policy / best-effort"; label proxies as proxies. (Reviews caught this 4× in Phase 1.)

## Status (2026-07-06)

Phase 0 (walking skeleton) + Phase 1 (state engine: recall → narrate → extract → gauntlet → commit → project; claim/belief epistemic layer; pgvector semantic memory; 4 provider adapters; thesis harness) are **code-complete, 71 tests green**. Next: the live thesis run — the owner chose the **OpenAI** path; step-by-step runbook is `docs/live-run.md`. After that, Phase 2 (branching timelines — the signature feature).
