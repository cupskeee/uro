# Uro — design & internals

This folder is the **authoritative design** for Uro. It's where the *why* lives — the architecture, the domain model, the decision log, and an honest map of what's proven vs. deferred. When code and docs disagree, one is fixed to match the other in the same commit.

New here? The [root README](../README.md) is the friendly, game-dev-first introduction. This page is the deep dive.

**Fastest way to grok the whole system:** [15-walkthroughs.md](15-walkthroughs.md) (life of a world, life of a beat, a Chronicler-mode war story).

## Reading order

- **`00`–`11`** — the design, in order (vision → architecture → domain → timeline → LLM → pipeline → rulesets → persistence → API → worlds → roadmap → open questions).
- **`12`–`15`** — the developer-facing contracts and guides; read before writing code.
- **`16`–`19`** — the honest capability map + the post-PoC designs.
- **`glossary.md`** pins the vocabulary (one line, one authority); **`decisions.md`** records why things are the way they are (ADR-style).

## Documentation map

| Doc | Contents |
|---|---|
| [00-vision.md](00-vision.md) | Vision, scope, the engine/platform boundary, personas |
| [01-architecture.md](01-architecture.md) | Package layout, subsystem map, ports & adapters (hexagonal) |
| [02-domain-model.md](02-domain-model.md) | Entities, epistemic claims/beliefs, NPC promotion tiers *(living doc)* |
| [03-timeline-and-branching.md](03-timeline-and-branching.md) | Event sourcing, snapshots, branches, the meteor test |
| [04-llm-integration.md](04-llm-integration.md) | Provider ports, auth (API key / Ollama / OpenAI-compatible), model roles, capability probes |
| [05-generation-pipeline.md](05-generation-pipeline.md) | Beat-based pipeline, modes (free-roam vs combat), promotion rules, dry-run sandbox *(living doc)* |
| [06-rulesets.md](06-rulesets.md) | Pluggable ruleset interface + the built-in d20 ("Uro Basic") and PbtA rulesets |
| [07-persistence-and-events.md](07-persistence-and-events.md) | Postgres-first storage, pgvector, edge tables, event bus, export packs |
| [08-api-and-sessions.md](08-api-and-sessions.md) | Server API, session model, multiplayer arbiters, the Chronicler outcome endpoint |
| [09-world-definition.md](09-world-definition.md) | World pack format, lore import + sufficiency check, prompt template packs |
| [10-roadmap.md](10-roadmap.md) | The PoC phases with acceptance tests + the post-PoC horizon |
| [11-open-questions.md](11-open-questions.md) | The brainstorm backlog — deliberately unresolved design areas |
| [12-event-catalog.md](12-event-catalog.md) | Canonical domain event types, payloads, emitter whitelist *(v0, living)* |
| [13-contracts.md](13-contracts.md) | BeatState, stage protocol, planner/extractor schemas, template contexts, failure semantics *(v0, living)* |
| [14-development-guide.md](14-development-guide.md) | Setup, tooling, migrations, testing, conventions, releasing + publishing to PyPI |
| [15-walkthroughs.md](15-walkthroughs.md) | End-to-end traces: life of a world, life of a beat, a Chronicler war story, doc responsibility map |
| [16-honesty-ledger.md](16-honesty-ledger.md) | The honest proven / proxy / stub-only / deferred map |
| [17-reaction-layer.md](17-reaction-layer.md) · [18-gap-findings.md](18-gap-findings.md) · [19-computation-tier.md](19-computation-tier.md) | Post-PoC designs: the reaction layer, the games' gap backlog, the computation tier |
| [decisions.md](decisions.md) · [glossary.md](glossary.md) | The ADR-style decision log (D-1..) · every term, one line each |
| [live-run.md](live-run.md) | How to reproduce the LLM-dependent legs with your own key |

## What's built (the build log)

**All five PoC phases, five post-PoC phases, the games-driven backlog, and distribution are complete** — 359 tests green, fully deterministic in CI (no live LLM calls; provider-dependent paths use recorded/mock transports or the deterministic stub). The core thesis was validated live *with caveats* — an ablation showed the full engine building durable state and re-surfacing an established NPC + world facts past the recency window while the `--bare` (raw-transcript) arm built nothing and drifted (it was **not** a clean "the bare model forgot the name" story — both arms stayed name-consistent independently). The honest capability map is [16-honesty-ledger.md](16-honesty-ledger.md).

Each phase ships a passing acceptance test:

- **P1 — state engine:** the beat pipeline (structured + semantic recall → narration → extraction → validation gauntlet → commit → projections) with a claim/belief epistemic layer, pgvector semantic memory, three provider adapters (stub / OpenAI-compatible / Anthropic), per-role routing.
- **P2 — branching timelines:** markers, snapshots, copy-on-fork, materialize-at-any-commit, per-branch PC binding, time-skip — the **meteor test** (one event log; continue / new-life / what-if forks coexist, no special-casing).
- **P3 — mechanics:** a pluggable, deterministic ruleset port + Uro Basic (d20, seeded RNG, encounter mode); a fight replays from its seed — the **insult→combat→consequences** acceptance.
- **P4 — worlds:** the world-pack format + sufficiency check + AI backfill, import + procedural History seeding (seed 42 ≠ 43 on identical geography), prompt-template packs, capability probes.
- **P5 — server & federation:** a WS play server (broadcast, token auth), export/import with hash-chain verification, off-screen belief/rumor propagation, and Chronicler mode — the **war-story test** (an external battle's feat ripples to witnesses; a tavern NPC retells a traceable rumor).
- **P6 — the alien ruleset:** a deliberately non-d20 second built-in (`uro_pbta`: 2d6, harm clock, moves) through the *same* port — proving the ruleset port is genuinely game-agnostic (D-30).
- **P7 — multiplayer:** per-participant PCs + a round-robin `PartyArbiter` behind the `TurnArbiter` port (D-31).
- **P8 — Chronicler hardening:** `distill_outcome` is trust-scoped — an external bundle can't kill/loot/first-hand-witness a PC or a higher-tier actor, loot needs real ownership, replays are idempotent (D-32).
- **P9 — the reaction layer:** pack-authored reactive behavior as *declarative data* (`rules.yaml` / `agendas.yaml`), never code — a closed grammar that can't name a mechanical/lethal/canon event (D-33).
- **P10 — the computation layer:** engine-owned, event-sourced integer counters that **fork by construction** (D-34).
- **The backlog-issues epoch (D-36–D-42):** the games-driven backlog, worked issue-by-issue — participant memory that survives a fork, a deterministic client-supplied-plan path, proposal/vote arbiter shapes + durable session tokens, multi-ref reaction scopes + a dropped-action audit, a trusted-embedder Chronicler tier for authored canon deaths, richer reaction grammar (`for_each` / `roll_table` / `expire_claims`), place/faction claim recall + cross-branch reads, and quantified/relational reaction triggers (`$trigger`-aware `when` + `per_event`).
- **Distribution (D-43):** dependency extras (base `uro-core` imports only ports), a docker-first quickstart, and trusted-publishing plumbing — the three packages are published to PyPI at **v0.2.0**.

Four games were built **on** the engine as forcing functions, producing an evidence-backed backlog ([18-gap-findings.md](18-gap-findings.md)) — which drove the last round of fixes and, just as usefully, *validated deferrals* (what **not** to build).

The live-model legs (thesis, planner, and the post-PoC phases) are run separately with an API key — CI never makes live LLM calls; world-pack AI backfill and capability probes are stub-tested only (never run live). Reproduce: [live-run.md](live-run.md) / `scripts/ablation.sh` (thesis) + `scripts/postpoc_validate.sh` (P6/P8).

## Architecture in one paragraph

Hexagonal by construction — the **core ring** (`domain`, `timeline`, `engines`, `pipeline`, `memory`, `session`, `chronicler`, `export`, `authored`, `_distill_core`) imports only *ports*, never a concrete adapter. That isn't a convention; it's a CI failure (import-linter) — which also *forbids* `uro_server` from importing the trusted `authored`/`_distill_core` distillation path (D-41: trust is which module you import). **Everything is an event:** state changes are typed domain events; projections are rebuildable read-models written only by the projector, in the same transaction as the commit; migrations are forward-only. Full detail: [01-architecture.md](01-architecture.md).

## Provenance

The project began from an LLM deep-research report that described a consumer *platform*. These docs re-scope it to an *engine* and fold in the owner's feedback; they are the single source of truth (`docs/` is authoritative — when code and docs disagree, one is fixed to match the other in the same commit).
