# Changelog

All notable changes to Uro Engine are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/) (pre-1.0: `0.MINOR.PATCH`, where a MINOR bump is a
notable or breaking change and a PATCH is a fix; see [docs/14](docs/14-development-guide.md)).
The authoritative design history lives in [`docs/decisions.md`](docs/decisions.md); the honest
capability map is [`docs/16-honesty-ledger.md`](docs/16-honesty-ledger.md).

## [Unreleased]

### Added
- **Timeline + inspection HTTP surface (BE-1…BE-5, #33–#37)** — the branch-graph, event-log, and
  dry-run/consistency endpoints, epic #44:
  - `GET /worlds/{w}/branches` (BE-1) — branch tree + markers + each branch's in-fiction day.
  - `GET /worlds/{w}/log[?branch=&limit=]` (BE-3) — commit lineage, git-log style.
  - `POST /worlds/{w}/branches {from_ref, name, time_skip_days?}` (BE-2) — fork from a commit/marker,
    with optional downtime-agenda time-skip (parity with `uro branch fork`).
  - `POST /worlds/{w}/markers {name, branch?}` (BE-3) — name a branch head.
  - `GET /worlds/{w}/events[?branch=&type=&entity_ref=&caused_by=&limit=]` (BE-4) — the raw event
    log along a branch, filterable; and `GET /worlds/{w}/commits/{id}` (BE-4) — one commit's events.
  - `POST /campaigns/{c}/dry-run {intent}` (BE-5) — preview the events a beat would commit, writing
    nothing (mirrors `uro dry-run`); any-authed, **intent-only** (no client `plan=`, D-37). And
    `GET /campaigns/{c}/consistency` (BE-5) — the narrator contradiction-survival proxy (T2).
  - **Authority:** the summary reads (`/branches`, `/log`) are any-authed; the structural writes
    (fork, marker-create) are **operator-only** (D-44) via a new `_require_operator` gate on the
    existing `is_admin` choke point; and the **raw event log** (`/events`, `/commits/{id}`) is
    **operator-only** (D-45 — it carries omniscient truth: `ClaimRecorded` truth-values, hidden
    beliefs, `caused_by`; never a player read). A new `advance_branch_time` server dep runs the
    fork's `--time-skip-days` via `engine.agenda_tick`.

### Fixed
- **PyPI publish workflow** — split into one job per package, each in its own GitHub environment
  (`pypi-core` / `pypi-server` / `pypi-cli`). A PyPI *pending* trusted publisher must be unique on
  `(owner, repo, workflow, environment)`, so all three sharing `environment: pypi` collided on setup.
  Owner setup updated in `docs/14` → "Publishing to PyPI". (D-43)

## [0.2.0] - 2026-07-19

### Added
- **Quantified/relational reaction triggers (RL-6, #25)** — a reaction rule can now react to "ANY
  member of faction X dying" (and the whole `$trigger`-bound predicate family): a `when` condition's
  entity-ref slots may be `$trigger.<field>`, bound from the triggering event's payload
  (`edge_exists(src="$trigger.actor_id", rel=member_of, dst=f:red-band)`). `when` is evaluated **per
  matching trigger event**, so it is a true existential over a multi-death beat — the naive
  "bind the first event" shape (which a design-check adversary showed misfires when a non-member dies
  first) is avoided. `trigger.per_event: true` fires the rule once **per** matching event (the
  count-each shape — e.g. `adjust_counter` per dead member; event-sourced, so it rides `fork_branch`,
  unlike a shadow game-code counter). Fences: `$trigger` is legal only in a ref slot of a rule (never
  a literal value slot or an agenda rule) and its field is validated against the event catalog at
  parse (the same check that fences `trigger.where`, and it must be a *string* field, not a list);
  an unbound-or-null ref fails the **whole** `when` closed. Read-only, so the action fence is
  untouched. `per_event` fan-out is bounded by the existing per-pass action cap (over-cap actions
  are audited). `RULES_API_VERSION` 4→5 (v1–v4 packs byte-identical). Resolves Ironwake's
  quantified-trigger gap-report row. (D-42)
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
- **Computation layer C3/C4/C5 — `for_each`, `roll_table`, `expire_claims` (#13)** — the reaction-layer
  rule grammar (`RULES_API_VERSION` 3→4; v1–v3 packs stay valid) gains: **`for_each`** (one bounded loop
  over a ref's edge-neighbors, with `$trigger.<field>` binding + `as`-var substitution, each neighbor
  scope-fenced); **`roll_table`** (a seeded, deterministic weighted pick → a chosen outcome's nested
  actions, baked so replay re-picks identically); and **`expire_claims`** (rumor decay — retract a stale
  module rumor to `truth=false`, migration 019 adds `created_day` to claims). All recursion is
  bounded (nested lists capped at parse; a shared per-pass node budget). Trust fences: a rule can never
  retract canon or reach a ref outside its scope through a binding. C6 (cascade) + computed-delta
  arithmetic stay staged (docs/19). (D-34)
- **Chronicler trusted-embedder ingestion tier + untrusted-path hardening (B6, #12)** — a Posture-A
  library embedder (which already holds root via `append_beat`) can now reuse outcome distillation
  *without* the D-32 protection ceiling via `uro_core.authored.distill_authored_outcome`, so an
  authored protected (T2+/PC) death commits as **real canon** and fires succession rules — unblocking
  assassination/succession games. **Trust is which module you import, never a wire flag**: an
  import-linter fence forbids the server from importing the ceiling-off path, and `OutcomeBundle` is
  now `extra='forbid'` + version-pinned (a forged `{trust:…}` field or unknown `v` → a loud 400).
  Untrusted-path hardening: an out-of-cast casualty now **drops** (was a public rumor); loot `to_ref`
  is protected (a bundle can't make a PC/named actor the recipient); the outcome endpoint enforces
  the D-39 campaign-scoped token. The untrusted parked-encounter registry stays reserved. (D-41)
- **Reaction-layer multi-ref scopes + dropped-action audit (B11, #11)** — a rule `scope` may now name
  several entities of one category (`factions: [a, b]` unions their members), the least-privilege
  middle ground between a single faction and the whole-`world` scope; a validator enforces exactly one
  jurisdiction. The action fence is unchanged (jurisdiction widened only). The rule gauntlet now
  produces a **dropped-action audit**: every refused action (out of scope, nonexistent target, a
  partial subject/witness filter, or over the per-pass cap) is recorded with a reason and logged —
  before, a fenced action vanished silently and an author couldn't tell a working rule from a no-op.
  `rules_api_version` bumped 2→3 (v1/v2 packs stay valid; a multi-ref pack declares v3). (D-40)
- **Session lifecycle: durable turn order + runtime tokens (B10, #10)** — refines D-31 (does not
  reverse it). Multiplayer **turn order is now reconnect/restart-stable**: the arbiter ring is seeded
  from durable PC-binding order (`store.pc_seats`, recovered from the `PCBound`/`PCReleased` log) — the
  turn cursor stays session-only (a durable cursor would ride `fork_branch`). **Runtime auth**: a
  durable, hashed (`sha256`), revocable, **campaign-scoped** `session_tokens` registry (migration 018,
  off the branch axis) lets a player added to a *running* server authenticate without a restart —
  minted via the authed `POST /join` (now returns `{actor_id, token}`), `POST /campaigns/{c}/tokens`,
  or `uro token mint`; revoked via `/tokens/revoke` or `uro token revoke`. `uro serve` gains
  `--admin-token` (an operator tier, distinct from ordinary `--token` players — only operators may act
  for others) and decouples `--arbiter` from the launch token count (so a runtime-added player still
  gets turns). New WS reject code `4403` (a minted token used on the wrong campaign). (D-39)
- **Small P3 follow-ups (B4/B5, #14)** — two evidenced, low-risk gap closures. **B4:** structured
  recall now surfaces a claim whose subject is an on-stage **place or faction** (`relevant()` unions
  on-stage `place_id`/`faction_id`), so a reaction-layer module rumor carrying a bare `p:`/`f:` ref
  (never a `name:` token) actually reaches the narrator when its entity is on stage — closing the
  P4×P9 seam. **B5 follow-up:** `store.current_world_time_batch(branch_ids)` returns each branch's
  in-fiction day in one recursive CTE (seeded from every head, carrying its origin), and `uro branch
  list` now prints each branch's `day=`. A third candidate — a fork-relative snapshot cadence for
  Hollowloop G-4 — was built, reviewed, and **reverted as a validated deferral**: the existing
  absolute cadence already bounds materialization replay to < N in every topology (`docs/18`).

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
- **Docker-first quickstart (#15, D-43)** — running any `uro` command against an unreachable
  database now prints one actionable line (`docker compose up -d --wait`, host port 5433, then
  `uro db migrate`; `URO_DATABASE_URL` to point elsewhere) instead of a raw driver traceback, via a
  single `connect_store` wrapper. Postgres + pgvector stays the one store — no second backend.
- **Dependency extras (#15, D-43)** — `uro-core`'s base install is now the pure engine (it imports
  only ports); the bundled adapters move behind extras: `uro-core[postgres]` (the Postgres + pgvector
  store), `uro-core[llm]` (the LLM provider adapters), `uro-core[all]`. `uro-cli` and `uro-server`
  pull both. Verified in a clean venv: the base install carries none of `asyncpg`/`pgvector`/`httpx`.
- **PyPI publishing plumbing (#15, D-43)** — an owner-activated `publish` workflow (trusted
  publishing / OIDC, no stored token) builds and uploads all three packages together; wheels now ship
  the LICENSE (`license-files`) and a per-package README as the PyPI long description. One-time
  pending-publisher setup and the run order are in `docs/14` → "Publishing to PyPI".

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
- `__version__` (and so `uro version`) now reads from installed package metadata on all three
  packages — it was hardcoded `0.0.1` and had drifted from the `0.1.0` in each `pyproject.toml`.

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

[Unreleased]: https://github.com/cupskeee/uro/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/cupskeee/uro/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/cupskeee/uro/releases/tag/v0.1.0
