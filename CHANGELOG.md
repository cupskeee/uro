# Changelog

All notable changes to Uro Engine are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/) (pre-1.0: `0.MINOR.PATCH`, where a MINOR bump is a
notable or breaking change and a PATCH is a fix; see [docs/14](docs/14-development-guide.md)).
The authoritative design history lives in [`docs/decisions.md`](docs/decisions.md); the honest
capability map is [`docs/16-honesty-ledger.md`](docs/16-honesty-ledger.md).

## [Unreleased]

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
- Initial design: re-scoped from a consumer *platform* to a headless *engine* (git:GitHub
  analogy). Decisions D-1…D-15 (see [`docs/decisions.md`](docs/decisions.md)).

[Unreleased]: https://github.com/cupskeee/uro/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/cupskeee/uro/releases/tag/v0.1.0
