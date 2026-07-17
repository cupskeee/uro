# Changelog

All notable changes to Uro Engine are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/) (pre-1.0: `0.MINOR.PATCH`, where a MINOR bump is a
notable or breaking change and a PATCH is a fix; see [docs/14](docs/14-development-guide.md)).
The authoritative design history lives in [`docs/decisions.md`](docs/decisions.md); the honest
capability map is [`docs/16-honesty-ledger.md`](docs/16-honesty-ledger.md).

## [Unreleased]

### Added
- **Participant memory (B8, #7)** — a player's out-of-world notes that **survive a fork** (time-loop /
  roguelike / NG+): a caller-owned `ParticipantMemory` lane keyed on `(participant_id, world_ref)`,
  deliberately outside the branch/projection axis so `fork_branch` never resets it. Surfaces to the
  narrator as the player's private recollection (never canon / never an NPC belief, by direct
  construction); `uro codex add/list`; migration 017. The event-sourced journal is reserved (D-36).
- **Client-supplied plan (B9, #8)** — `Engine.run_beat/run_beat_stream/preview_beat(..., plan=BeatPlan)`
  drives the planner→mechanics gate **deterministically, with no LLM** (CI mechanics coverage + keyless
  consumers): free-roam checks *and* full encounters resolve from an injected plan. The supplied plan is
  fenced by the same `validate_plan` as an LLM plan (unknown affordance → `PlannerError`) and is a
  **trusted in-process input** — no D-32 ceiling (that fences the external Chronicler POST); a future
  network-exposed `plan=` must add it (D-37). `BeatResult` gains `check_traces` (per-check detail — incl.
  a resolved fight's rounds). Library API only for now (not wired to `serve`/CLI).
- **Arbiter shapes beyond round-robin (B7, #9)** — two new multiplayer turn shapes behind the same
  `TurnArbiter` port, plus a **non-canon coordination lane**: `uro serve --arbiter proposal` (a
  non-holder's intent is surfaced to the table as a proposal via the now-live `AdmitDecision.QUEUED`,
  not a silent refusal) and `--arbiter vote` (a session-only consensus tally). `uro connect` gains
  `/say <text>` (table-talk) and `/vote <choice>` — both **non-canon by construction** (broadcast via
  the session hub, never reaching a commit), so no proposal/debate/vote burns a canonical beat. New WS
  frames: `table_talk`, `proposal_opened`, `vote_tally`, `vote_decided`, `vote_unsupported`. All
  session-only (D-31), zero events/migrations. Consensual-PvP / simultaneous / reactive-interrupt stay
  reserved behind the same port (D-38).

### Changed
- **Repositioned Uro as its own thing — "a world-state engine" — and retired the git→GitHub analogy**
  from all outward-facing framing (README, `CLAUDE.md`, social preview, docs 00/01/03/08/10/glossary).
  New tagline: *"Worlds that remember. Timelines that fork."* The analogy was a comprehension scaffold,
  not the product's identity (D-35, refines D-1; D-1/D-30 keep their historical text).
- Added the logo as the README header + a branded 1280×640 social-preview image
  (`docs/images/`, regenerable from `social-preview.html`).

### Added
- `py.typed` markers on all three packages (PEP 561 — ship types to embedding consumers).
- Public package metadata in each `pyproject.toml` (`[project.urls]`, authors; keywords on uro-core).
- Least-privilege `permissions: contents: read` on the CI workflow.

### Fixed
- Reconciled `docs/16-honesty-ledger.md` and `docs/10-roadmap.md` to cover Phase 10 (the computation
  layer, D-34) — they had stopped at Phase 9 and still described counters as "refused".
- README "four provider adapters" → accurate (three adapters / four provider kinds); added a
  `just`-less Quickstart fallback and tagged shell code fences as `sh`.
- Regenerated `uv.lock` (was stale at 0.0.1 after the 0.1.0 bump).
- Removed the dead GitHub-Discussions contact link's leaked TODO; enabled Discussions +
  private-vulnerability-reporting + secret-scanning on the repo so SECURITY.md's channel works.
- `just release` now checks all three package versions, not just uro-core.
- Guarded a stray root `data` file (PII) in `.gitignore`.

## [0.1.0] - 2026-07-13

First tagged release — the complete proof of concept, made public. The dated **development
milestones** below (P1–P10) are the pre-1.0 history folded into this release.

### Added
- The full engine: five PoC phases + five post-PoC phases (P1–P10) — see the milestones below.
- Public-readiness: MIT `LICENSE` + `license = "MIT"` in every package's metadata; community
  health files (`CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, issue & PR templates);
  README badges + an architecture/tests overview; a release workflow + a documented versioning
  and release process.
- **G-3 — reproducible mechanics RNG:** `_beat_rng` derives from the campaign's persisted seed +
  commit depth (both deterministic) instead of random ids, so a played-through fight replays
  given the same event log. `uro campaign new --seed`; seed persisted (migration 016).

### Removed
- The initial LLM deep-research report was removed from the repo (and its history) before going
  public; the `docs/` set is the single source of truth.

## Development milestones (pre-1.0)

## 2026-07-12 — Post-PoC: the computation layer + the dogfood backlog

### Added
- **Phase 10 — computation layer (D-34):** engine-owned, event-sourced integer counters
  (`CounterChanged` → `proj_counters`, migration 015) that **fork by construction**, plus
  cross-entity compare and edge-count conditions; a `world` scope.
- **`Engine.append_and_react`** — the one-call authored-commit path (commit + react).
- Four games built **on** the engine as forcing functions, synthesized into an evidence-backed
  backlog ([`docs/18-gap-findings.md`](docs/18-gap-findings.md)).
- Backlog items: place-state recall (B4), cross-branch query surface (B5), a Chronicler
  ingestion receipt (B6), and an authed REST management surface (B3).

### Fixed
- A fork hot-path sequential scan (missing `memory_index(commit_id)` index, migration 014), a
  `PartyArbiter` holder-disconnect misrotation, accepted-but-inert rule triggers, and silent
  rule-pack death — all surfaced by the games.

## 2026-07-10 — Post-PoC: the reaction layer (D-33)

### Added
- **Phase 9 — reaction layer:** pack-authored reactive behavior as *declarative data*
  (`rules.yaml` / `agendas.yaml`), never code — a closed grammar that structurally cannot name a
  mechanical, lethal, or protected-canon event; a post-beat `react()` hook and a downtime
  `agenda_tick`.

## 2026-07-08 — Post-PoC phases 6–8 + live validation

### Added
- **Phase 6 — the alien ruleset (D-30):** a non-d20 second built-in (`uro_pbta`) through the
  same ruleset port, proving it game-agnostic.
- **Phase 7 — multiplayer (D-31):** per-participant PCs + a round-robin `PartyArbiter` behind
  the `TurnArbiter` port.
- **Phase 8 — Chronicler ingestion hardening (D-32):** `distill_outcome` is trust-scoped — an
  external bundle can't kill/loot/first-hand-witness a PC or a T2+ actor; idempotent replays.
- Per-role model routing; the honesty ledger ([`docs/16`](docs/16-honesty-ledger.md)).

### Verified
- The core thesis was validated live (with caveats) via an ablation run; the alien-ruleset and
  Chronicler legs were validated end-to-end.

## 2026-07-07 — The five-phase PoC is code-complete

### Added
- **P1 state engine** (recall → narrate → extract → gauntlet → commit → project; claims/beliefs;
  pgvector memory), **P2 branching timelines** (the meteor test), **P3 mechanics** (ruleset port
  + Uro Basic d20; seeded encounters), **P4 worlds** (packs, import, procedural history seeding,
  probes), **P5 server & federation** (WS play, export/import with hash-chain verification,
  belief propagation, Chronicler mode — the war-story test).

## 2026-07-04 — Scoping

### Added
- Initial design: re-scoped from a consumer *platform* to a headless *engine* (the engine/platform
  boundary). Decisions D-1…D-15 (see [`docs/decisions.md`](docs/decisions.md)).

[Unreleased]: https://github.com/cupskeee/uro/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/cupskeee/uro/releases/tag/v0.1.0
