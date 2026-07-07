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

Build each increment as one coherent, tightly-coupled slice **directly** (don't fan out — layers interlock), run `just test` green, and **commit it**. **Review at the END of the PHASE, not after each increment** (owner directive, 2026-07-07): build all of a phase's increments first, then run ONE **multi-dimension adversarial review** via the Workflow tool over the *whole phase* (5 dimensions → find → verify each against the ACTUAL code, default REJECTED; + a completeness-critic pass). This catches the cross-increment **seams** and whole-phase spec/doc drift a per-increment diff can't see — the real payoff of reviewing at phase end. Triage verified verdicts; fix confirmed-real, defer verifier-agreed non-issues (name them in the commit); reconcile any doc/code drift in the same commit. The review reliably finds a real edge-case bug (and doc-overclaim) per phase, outside the happy path the tests cover. Prior review scripts are under the session workflow dir.

**Watch for overclaiming:** trace whether the code actually enforces a guarantee before writing it; prefer "by construction (structural)" vs "by policy / best-effort"; label proxies as proxies. (Reviews caught this 4× in Phase 1.)

## Status (2026-07-07)

Phase 0 + Phase 1 (state engine: recall → narrate → extract → gauntlet → commit → project; claim/belief epistemic layer; pgvector semantic memory; 4 provider adapters; thesis harness) are **code-complete**. The **thesis was validated live** via OpenAI — the ablation (`scripts/ablation.sh`, `docs/live-run.md`) showed the full engine recalling a named NPC + plot fact past the recency window while `--bare` forgot them. That run also surfaced extractor over-extraction (flavor → `truth=true`), fixed in Phase 1.5. **Live runs:** the owner's `OPENAI_API_KEY` is not visible to Claude's bash — the owner runs keyed commands, Claude analyzes results from Postgres.

**Phase 2 (the signature phase — branching timelines) is code-complete, 91 tests green, fully deterministic (no key needed). The meteor acceptance test PASSES** (`tests/test_meteor.py::test_the_meteor_test`): one played campaign ends with a player-caused `PlaceDestroyed(Vel)`, and from the SAME event log — continue (adopt the wizard PC, face the crater), new-life (fresh farmer a year later via time-skip; NPCs retell the deed as history; the wizard is now a legend/NPC), and what-if (forked pre-strike, Vel stands) all coexist, no special-casing.

- **inc 2.1 (substrate):** commit-depth, `markers`, `snapshots` + materialization-at-any-commit (nearest-snapshot + replay), copy-on-fork branching (`fork_branch` — projections rebuilt at the fork commit, memory-index rows copied, embeddings shared by content-hash), the `proj_places` projection, CLI `uro branch fork/list/mark` + `uro log`.
- **inc 2.2 (fork semantics):** PC binding (`proj_pcs`, `PCBound`/`PCReleased`, `is_pc`/`active_pcs` — PC-ness is per-branch, the same actor is a PC on one fork and an NPC on a sibling), campaign lifecycle (`start_campaign` adopt-or-fresh, `end_campaign` releases PCs + marks the fork root), deterministic time-skip (`TimeAdvanced` + honest `AdaptationApplied` header, no LLM ripple), CLI `uro campaign new/end` + `uro branch fork --time-skip-days`.

Each increment got a 5-dimension adversarial-review workflow (find→verify, default-rejected): 2.1 → 4 fixes (branch-name `UNIQUE`, `create_marker FOR UPDATE`, `PlaceStateChanged` enum bar); 2.2 → 1 fix (acceptance test now asserts an active PC binding survives a fork — guarding the snapshot `pcs` path).

**Phase 3 (mechanics) is COMPLETE — code + a phase-end holistic review, 126 tests green, deterministic (Claude builds AND verifies with a stub planner; the live planner run is the owner's, like Phase 1). The acceptance test PASSES** (`tests/test_encounter.py::test_the_acceptance_insult_to_combat_to_consequences`): a free-roam insult, then an attack triggers the pipeline's mode transition to combat, a multi-round fight auto-resolves under Uro Basic (a pure function of the seed — same seed → byte-identical replay; a seed-sweep test asserts the acceptance's PC-loses outcome holds across many seeds), and the lost fight leaves persistent consequences — the PC downed (hp 0), a `truth=true` injury claim, and a looted item (`ItemTransferred`) — visible in later free-roam and carried on a fork.
- **inc 3.1 (ruleset port + Uro Basic):** seeded `Rng`, the `Ruleset` Protocol (`rulesets/base`) + value types, and Uro Basic (`rulesets/uro_basic`) — d20 checks, the encounter state machine, deterministic NPC AI (D-26), effects, level 1-5 progression. A fight replays byte-identically from one seed. Review: 19 → 6 fixed.
- **inc 3.2 (planned free-roam pipeline, D-28):** the planner stage (schema-forced `BeatPlan`) + deterministic plan validation (affordance fence + D-21 trigger coverage), the mechanics gate (ruleset checks + a per-beat seeded `Rng`), character sheets as state (`SheetUpdated`, `proj_sheets`). No ruleset bound → unchanged Phase-1 flow. Review: 19 → 10 fixed.
- **inc 3.3 (encounter mode):** mode transitions (attack → encounter), the auto-resolved initiative loop (`pipeline/encounter.py`, D-29 — narrows D-26: both sides via `npc_action`, whole fight in one beat; interactive per-turn play deferred), effects→R-events→projections (`ActorDamaged`→hp, `ItemTransferred`→`proj_items`), lost-fight consequences. Review: 12 → 4 fixed (encounter now requires 2 distinct known opponents on opposing teams, else falls back to free-roam; D-29 doc-reconciliation).
- **Phase-end holistic review** (whole phase, 5 dims + completeness critic): 17 candidates → 8 fixed, **0 code defects** — the loot half went live (`ItemCreated` now emitted by `start_campaign` as starting gear, so a lost fight loots real items in play), and consequence-gating / "move+action" / the determinism claim were reconciled from overclaim to honest. (Per-increment reviews are now folded into this one phase-end pass — owner directive; see "How increments get built".)

**Phase 4 (worlds) is COMPLETE — code + a phase-end holistic review, 148 tests green, every acceptance leg demonstrated** (deterministic legs fully; the LLM legs — backfill/probe — stub-tested, live pass pending like Phases 1/3). The acceptance runs end-to-end across `worlds/ashfall` (rich → runnable) and `worlds/thornwood` (thin): `validate` flags Thornwood's missing conflict seed → `create --backfill` fills it and **commits it as a `ThreadCreated` tagged `provenance=ai_backfill`** (queryable in `proj_threads`) → `probe` passes → `seed 42` vs `seed 43` produce different dynasties/wars on identical authored geography → a campaign narrates in the pack's tone.
- **inc 4.1 (pack format):** `worldpack/{models,parse,sufficiency}` — Pydantic manifest+seed schemas, `parse_pack`, the sufficiency rubric (runnable|thin|insufficient + gaps). CLI `uro world validate`; two example packs.
- **inc 4.2 (import + seeding):** factions + the graph edge table (migration 010, deferred since P1) come due; `pack_to_events` (emitter S) commits authored geography at `WorldGenesis`; `engines/history.seed_history` procedurally generates dynasties/wars (emitter H) — pure fn of (manifest, seed), byte-identical replay, seed-varying. CLI `uro world create` / `uro world seed`.
- **inc 4.3 (prompt packs, D-6):** default Jinja2 templates in `uro_core/prompts/` + pack override-by-filename (`PromptEnv`); the manifest tone flows to the narrator (`WorldGenesis` stores tone+overrides; the engine loads per-branch `world_style`). The three stage prompts are now templates.
- **inc 4.4 (creator loop):** `worldpack/backfill` (opt-in AI gap-fill, provenance-tagged), `engines/probe` (structured_output gate + content_rating, judge-scored D-24, warn-not-fail), `Engine.preview_beat` (dry-run — full pipeline, no commit). CLI `uro world backfill` / `uro world probe` / `uro dry-run`.
- **Deferred (honest):** LLM lore-extraction (authored YAML is the primary source); backfill fills only the *conflict* dimension (others extend the same pattern); persisted/timestamped probe-report storage (PoC prints with transcripts); per-commit temporal edge validity (docs/07 — PoC does current-state edges); the rest of the docs/04 probe suite (context_window/instruction_following/consistency/latency) — same ask→judge pattern; the `TEMPLATE_API_VERSION` pin (constant reserved, not enforced).
- **Phase-end holistic review** (whole phase, 5 dims + completeness critic): 22 candidates → 18 confirmed → triaged. **Real seam fixed:** the backfill→import chain was severed (backfill print-only, threads never imported) so `provenance=ai_backfill` reached no event — now threads commit as `ThreadCreated` (emitter S, `proj_threads`, migration 011) and `create --backfill` wires backfill into import, making the acceptance's backfill leg real. Also: snapshot ordering now total (all columns, not first-two), sufficiency checks the runtime template name, `PromptEnv` is `StrictUndefined`. The rest were **doc-drift reconciled** in the same commit (docs 04/07/09/12/13/15: prompt-pack filenames, probe "stored report", emitter-whitelist framing, WorldGenesis payload, factions/edges/threads now Built, seeding-scale honesty).
- Next: **Phase 5 — federation / multiplayer** (participants, Chronicler-mode async encounters, the federation seam).
