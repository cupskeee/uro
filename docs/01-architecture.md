# 01 — Architecture

## Shape of the deliverable

Three artifacts in one monorepo, mirroring the git model (plumbing library + porcelain):

```
uro/
├── packages/
│   ├── uro-core/          # ALL engine logic. Pure Python library, embeddable.
│   │   └── uro_core/
│   │       ├── domain/        # entities, events, value objects (Pydantic models)
│   │       ├── timeline/      # event store, snapshots, branching
│   │       ├── engines/       # world, history, actor, narration services
│   │       ├── pipeline/      # beat pipeline: stages, planner, canonicalizer
│   │       ├── rulesets/      # ruleset PORT (+ auth); built-in impl in rulesets/uro_basic/
│   │       ├── providers/     # provider PORT + auth strategies; concrete adapters in providers/adapters/
│   │       ├── memory/        # retrieval: embeddings, recall triggers
│   │       ├── worldpack/     # world definition parsing, import, sufficiency check
│   │       ├── ports/         # persistence & event-bus interfaces (no impl)
│   │       └── adapters/      # postgres, pgvector, in-proc bus implementations
│   ├── uro-server/        # Thin FastAPI wrapper: REST + WebSocket, sessions
│   └── uro-cli/           # Reference client: play loop, dev tools (dry-run, probe, validate)
├── worlds/                # example world packs (test fixtures + demo content)
└── docs/
```

- **Runtime:** Python 3.12+, FastAPI, Pydantic v2. Async throughout (the workload is I/O-bound LLM orchestration).
- **Dependency rule (hexagonal):** `uro_core.domain`, `timeline`, `engines`, `pipeline`, `memory` import **no** framework or driver code — no FastAPI, no SQLAlchemy/psycopg, no provider SDKs. They depend only on **port interfaces** (persistence/bus in `ports/`, the provider port in `providers/`, the ruleset port in `rulesets/`), never on concrete adapters (`adapters/`, `providers/adapters/`, `rulesets/uro_basic/`). Adapters implement ports. `uro-server` and `uro-cli` wire adapters to the core. This is what keeps the engine embeddable and every infrastructure choice swappable — and it's enforced in CI by import-linter (`14`, D-27), including the provider/ruleset sub-boundary that a leaf-library ban alone would miss.
- **Preferred-language note:** C++/C# were considered and deferred for MVP velocity; the hexagonal boundary means a future port rewrites adapters and transport, not the design.

## Subsystem map

The four "engines" from the research report survive as **core services**, with corrected semantics:

```
                       ┌────────────────────────────────────────────┐
                       │                uro-server / uro-cli         │
                       │        (transport, sessions, wiring)        │
                       └──────────────────────┬─────────────────────┘
                                              │
                    ┌─────────────────────────▼──────────────────────────┐
                    │              Beat Pipeline (pipeline/)              │
                    │  context → plan → generate → mechanics → narrate    │
                    │            → extract/canonicalize                   │
                    └──┬──────────┬──────────┬──────────┬────────────┬───┘
                       │          │          │          │            │
                ┌──────▼───┐ ┌────▼─────┐ ┌──▼───────┐ ┌▼─────────┐ ┌▼────────┐
                │  World   │ │ History  │ │  Actor   │ │ Ruleset  │ │ Provider│
                │  service │ │ service  │ │ service  │ │ (plugin) │ │ router  │
                └──────┬───┘ └────┬─────┘ └──┬───────┘ └┬─────────┘ └┬────────┘
                       │          │          │          │            │
                    ┌──▼──────────▼──────────▼──────────▼───┐   ┌────▼────────┐
                    │        Timeline (event store,          │   │  LLM        │
                    │        snapshots, branches)            │   │  adapters   │
                    └──────────────────┬─────────────────────┘   └─────────────┘
                                       │
                              ┌────────▼────────┐
                              │ Postgres (+ pg- │
                              │ vector, edges)  │
                              └─────────────────┘
```

### World service (`engines/world`)
Owns the physical and cosmological layer: geography, climate, places, resources, magic/cosmology rules. **Correction to the report:** this layer is *not* immutable. It is **slow-changing, not static** — a meteor strike, a flood, a razed city are physical events on the timeline like any other. "Static" only means: within a single moment of the timeline, this layer is fixed context for generation. New worlds can be authored (via world packs) or generated with different characteristics.

### History service (`engines/history`)
Two jobs: (1) **seeding** — given a world pack, simulate/generate the backstory up to the campaign start (dynasties, wars, cataclysms) and write it as pre-play events on the timeline; (2) **adaptation** — when play changes the world (king assassinated, plague released), regenerate affected forward-looking narrative threads. History is the *author of context*; the timeline is the *record*.

### Actor service (`engines/actor`)
NPCs and factions with beliefs, goals, relationships, and memory. Two critical properties:
- **Lazy promotion:** actors exist at tiers from "background extra" to "full agent" and are promoted when play makes them matter — because players can decide a random tavern regular is important. See `02-domain-model.md`.
- **Off-screen life:** actors advance when unobserved. MVP strategy is **simulate-on-observation** (compute what happened since last seen, when next seen) rather than continuous background ticking — same narrative effect, a fraction of the compute. See `11-open-questions.md` (OQ-4).

### Narration service (`engines/narration`)
The player-facing text layer: scene description, dialogue, outcome prose. Consumes structured facts from the other services, style/tone from the world's prompt pack, and mechanics results from the ruleset. **No fixed "turn" in free-roam** — narration is beat-based; structured turns exist only inside combat/encounter mode, owned by the ruleset. See `05-generation-pipeline.md`.

### Ruleset (plugin)
Mechanics live behind a port: character sheets, checks, combat state machine, progression. The engine ships "Uro Basic" (minimal d20) as the default plugin. See `06-rulesets.md`.

### Provider router (`providers/`)
Role-based LLM routing (narrator, dialogue, planner, extractor, summarizer → concrete model bindings per world/deployment), multiple auth strategies, capability probes. See `04-llm-integration.md`.

### Timeline (`timeline/`)
The spine of the whole engine: append-only event log per world, snapshots, branch refs. Everything every other service "knows" is a projection of this log. See `03-timeline-and-branching.md`.

## Cross-cutting rules

1. **All state change is an event.** Services never mutate a projection directly; they emit domain events, projections update from the log. This is non-negotiable — branching, replay, dry-run, and the Lore Wall all depend on it.
2. **Dry-run is a first-class mode.** Any pipeline execution can run against a branch head without committing; output includes the would-be events as a diff. The CLI exposes this (`uro dry-run`). This is the creator "testing sandbox."
3. **Determinism where possible.** Seeded RNG for rolls and procedural steps; LLM calls are recorded (prompt hash → response) so a beat can be re-examined. Full replay determinism across LLM calls is impossible; recorded-response replay is the pragmatic substitute.
4. **The engine has no opinion on content** and no moderation stage. See `00-vision.md`.
5. **Usage metering everywhere.** Every provider call records tokens/latency/model tagged by pipeline stage, queryable via API — the engine doesn't care about cost, but it must let consumers care.
