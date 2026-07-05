# 10 — Roadmap

Solo-dev PoC roadmap. No calendar estimates (the report's quarter/budget tables assumed a staffed company and are disregarded — D-14). Phases are strictly ordered by dependency; each has a demoable **acceptance test** and explicit non-goals. A phase is done when its test passes, not when its code feels finished.

## Phase 0 — Walking skeleton

*The thinnest playable end-to-end slice: prove the shape.*

- Monorepo scaffolding (`uro-core` / `uro-server` / `uro-cli`), Docker Compose Postgres, CI with tests — everything per `14-development-guide.md` (uv workspace, migrations, import-linter, recorded-provider test harness).
- Domain + timeline minimal core: events, commits, one branch, no snapshots/forking yet.
- Provider port + `openai_compat` adapter (covers Ollama for free) + role router with a single binding.
- Degenerate pipeline: context (recency only) → narrate → commit raw beat log. No planner, no extraction, no mechanics.
- `uro play` against a hardcoded fixture world.

**Acceptance:** play 20 coherent beats in a tavern over two separate CLI sessions; the second session resumes exactly where the first ended (events reloaded, not chat history).

**Non-goals:** branching, mechanics, world packs, server, structured state.

## Phase 1 — State that matters

*From chat log to world model.*

- Full entity projections (actors, places, claims, edges, beliefs), actor tiers T0–T2.
- Planner and extractor stages live; promotion rules; contradiction-rejection on canonicalize.
- Retrieval: structured recall + pgvector semantic recall + summarizer compression.
- `anthropic` adapter; multi-role model bindings.

**Acceptance:** an NPC lies to the player; ten beats later, a *different* NPC contradicts the lie from `truth=true` state — and a claim first mentioned ~50 beats ago resurfaces correctly via recall.

**Non-goals:** combat, forking, off-screen simulation.

## Phase 2 — Timeline ★ the signature phase

*The reason this engine exists.*

- Snapshots, markers, branch-from-any-commit, materialization at arbitrary commits.
- Fork semantics: carry/drop rules, adopt-existing-actor-as-PC, time-skip on fork (History adaptation pass).
- `uro branch fork`, `uro log`; campaign-over-branch plumbing.

**Acceptance: the meteor test** (`03-timeline-and-branching.md`) — one played campaign ending in a city-destroying event, then (a) continue as the same character, (b) new campaign as a farmer in the aftermath who hears NPCs retell campaign A's deeds as history, (c) a what-if branch from before the event. All three from the same event log, no special-casing.

**Non-goals:** export packs, server.

## Thesis validation (runs alongside Phases 1–2)

The phases prove the *machine* works; nothing in them proves the *bet* — that state-tracked narration beats a long-context chat log. Two checks, cheap and mandatory (reinstated from the research report's experiments section, which these docs originally deleted):

- **T1 — Ablation.** The same scenario, world, and seed played two ways: (a) the full engine; (b) the same narrator model with a raw rolling transcript — no state, no recall, no extraction. Blind-compare transcripts (yourself + 2–3 volunteers) for continuity errors and preference at 30+ beats. **Kill criterion:** if (b) is indistinguishable, stop building and rethink — that's cheaper to learn at Phase 1 than at Phase 5.
- **T2 — Fact-consistency metric.** Percent of narration-asserted, state-checkable claims per beat that agree with projections (computed by cross-checking extractor output against state; logged continuously from Phase 1). Target ≥90% (placeholder, `11`); a downward trend after any pipeline or model change is treated as a regression gate.

## Phase 3 — Mechanics

- Ruleset port + Uro Basic; mechanics gate stage; affordance-prompted planner.
- Encounter mode: initiative, turn loop, effects-as-events; mode transitions.
- Encounter completion is **async-capable from day one**: an encounter can be parked and later resolved by an out-of-band outcome bundle (the Chronicler-mode door, D-25) — even though this phase only uses in-process resolution.
- Seeded RNG discipline end-to-end; recorded-response replay for beat debugging.

**Acceptance:** a free-roam insult escalates into combat (mode transition decided by pipeline), a three-round fight resolves under Uro Basic with narration weaving real roll results, and a lost fight leaves persistent consequences (injury claim, item looted) visible in later free-roam.

## Phase 4 — Worlds

- World pack format: parsing, validation, entity extraction from lore, **sufficiency check**, AI backfill (opt-in, provenance-tagged).
- Prompt template packs with override-and-fallthrough; History seeding from manifest (`simulate_years`).
- **Capability probes** + stored reports; dry-run mode polished (`uro dry-run`, event diffs).
- Two real example packs in `worlds/` (one rich, one deliberately thin — the thin one is the sufficiency-check fixture).

**Acceptance:** author a fresh pack from scratch, `validate` flags a missing conflict seed, backfill fills it (tagged), `probe` passes, `seed --seed 42` then `seed --seed 43` produce visibly different histories on identical geography, and a campaign plays in the authored tone.

## Phase 5 — Server & sessions

- `uro-server`: full REST surface, WebSocket play channel, SSE beats, token auth mode.
- Session/participant model with `SoloArbiter`; CLI HTTP-client mode.
- Export/import packs (world, branch-at-commit) with hash-chain verification.
- **Off-screen belief/rumor propagation** (the war-story ripple depends on it; it is scheduled here, not assumed): Actor-service simulate-on-observation emits `BeliefChanged` fan-out along contact edges with per-hop confidence/detail decay, **tier-agnostic** so an ordinary downstream NPC can acquire and retell a rumor (OQ-4). This is also the roadmap's first build of off-screen agenda resolution.
- **Proof of Chronicler mode:** the outcome-bundle endpoint, bundle schema v0 (participants, witnesses, casualties, notable feats, loot, duration), rule-based distillation into the standard gauntlet — and a ~50-line toy auto-battler script as the external "game."

**Acceptance:** two CLI clients (two tokens) attached to one campaign both receive the same streamed beats; a world exported from one machine imports and continues on another. Plus **the war-story test**: a toy external battle in which the PC's spectacular feat has surviving enemy witnesses — beats later, a tavern NPC retells a distorted version of it, with the belief chain traceable back to those witnesses; re-run the same battle with zero survivors and nobody ever mentions it.

## Post-PoC horizon (unordered, deliberately unscheduled)

Multiplayer `PartyArbiter` (OQ-7) · full Chronicler-mode ingestion contract beyond the toy proof (OQ-12) · module/scripting system for packs · graph/vector store swap-ins if scale demands · additional rulesets (`srd51`) · NATS-backed distribution · subscription-OAuth auth strategies if ever reconsidered (removed at D-16) · anything platform-shaped (which is someone else's repo).

## Standing engineering practices (all phases)

- Tests ride along, not after: domain logic unit-tested; pipeline stages tested with recorded LLM responses; each phase's acceptance test automated as far as LLM nondeterminism allows (assert on *events*, not prose).
- Every LLM call metered and stage-tagged from Phase 0 — retrofitting observability is misery.
- `docs/` updated in the same commit as the behavior it describes; decisions appended to `decisions.md` when made, including reversals.
