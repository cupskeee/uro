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

## Status (2026-07-07)

Phase 0 + Phase 1 (state engine: recall → narrate → extract → gauntlet → commit → project; claim/belief epistemic layer; pgvector semantic memory; 4 provider adapters; thesis harness) are **code-complete**. The **thesis was validated live** via OpenAI — the ablation (`scripts/ablation.sh`, `docs/live-run.md`) showed the full engine recalling a named NPC + plot fact past the recency window while `--bare` forgot them. That run also surfaced extractor over-extraction (flavor → `truth=true`), fixed in Phase 1.5. **Live runs:** the owner's `OPENAI_API_KEY` is not visible to Claude's bash — the owner runs keyed commands, Claude analyzes results from Postgres.

**Phase 2 (the signature phase — branching timelines) is code-complete, 91 tests green, fully deterministic (no key needed). The meteor acceptance test PASSES** (`tests/test_meteor.py::test_the_meteor_test`): one played campaign ends with a player-caused `PlaceDestroyed(Vel)`, and from the SAME event log — continue (adopt the wizard PC, face the crater), new-life (fresh farmer a year later via time-skip; NPCs retell the deed as history; the wizard is now a legend/NPC), and what-if (forked pre-strike, Vel stands) all coexist, no special-casing.

- **inc 2.1 (substrate):** commit-depth, `markers`, `snapshots` + materialization-at-any-commit (nearest-snapshot + replay), copy-on-fork branching (`fork_branch` — projections rebuilt at the fork commit, memory-index rows copied, embeddings shared by content-hash), the `proj_places` projection, CLI `uro branch fork/list/mark` + `uro log`.
- **inc 2.2 (fork semantics):** PC binding (`proj_pcs`, `PCBound`/`PCReleased`, `is_pc`/`active_pcs` — PC-ness is per-branch, the same actor is a PC on one fork and an NPC on a sibling), campaign lifecycle (`start_campaign` adopt-or-fresh, `end_campaign` releases PCs + marks the fork root), deterministic time-skip (`TimeAdvanced` + honest `AdaptationApplied` header, no LLM ripple), CLI `uro campaign new/end` + `uro branch fork --time-skip-days`.

Each increment got a 5-dimension adversarial-review workflow (find→verify, default-rejected): 2.1 → 4 fixes (branch-name `UNIQUE`, `create_marker FOR UPDATE`, `PlaceStateChanged` enum bar); 2.2 → 1 fix (acceptance test now asserts an active PC binding survives a fork — guarding the snapshot `pcs` path).

**Phase 3 (mechanics) is in progress, 119 tests green, deterministic (Claude builds AND verifies with a stub planner; the live planner run is the owner's, like Phase 1).**
- **inc 3.1 (ruleset port + Uro Basic):** seeded `Rng`, the `Ruleset` Protocol (`rulesets/base`) + value types, and Uro Basic (`rulesets/uro_basic`) — d20 checks, the encounter state machine (`start_encounter`/`legal_actions`/`npc_action`/`resolve_action`/`is_over`), deterministic NPC AI (D-26), effects, level 1-5 progression. A fight replays byte-identically from one seed. Review: 19 → 6 fixed.
- **inc 3.2 (planned free-roam pipeline, D-28):** the planner stage (schema-forced `BeatPlan`) + deterministic plan validation (affordance fence + D-21 trigger coverage), the mechanics gate (resolve free-roam checks via the ruleset + a per-beat seeded `Rng`), character sheets as state (`SheetUpdated`, `proj_sheets`, `start_campaign` assigns a sheet). No ruleset bound → unchanged Phase-1 flow. Review: 19 → 10 fixed (adopt-path PC now sheeted; the rest doc-honesty).
- Next: **inc 3.3 — encounter mode + the acceptance test** (mode transitions, the initiative turn-loop, effects→domain-events→projections for HP/items, the insult→combat→3-round-fight→lost-fight-consequences acceptance).
