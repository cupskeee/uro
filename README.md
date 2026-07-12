# Uro Engine

**git, but for AI-driven RPG worlds.** · _Versioned, forkable world state as a headless engine._

[![CI](https://github.com/cupskeee/uro/actions/workflows/ci.yml/badge.svg)](https://github.com/cupskeee/uro/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.12](https://img.shields.io/badge/Python-3.12-blue)
![Postgres 17 + pgvector](https://img.shields.io/badge/Postgres-17%20%2B%20pgvector-blue)
![Status: proof of concept](https://img.shields.io/badge/status-proof--of--concept-orange)

**Uro is a persistent, AI-driven RPG world *engine*.** It is not a game and not a platform. It is the headless core that games and platforms get built on top of — the way git is the engine underneath GitHub, GitLab, and Bitbucket.

The engine's one unique power: **world state is versioned and forkable**. Every campaign leaves a permanent mark on its world's timeline. A finished campaign's ending — political borders, factions, religions, wars, even physical changes like a meteor crater where a city used to be — becomes a point in history that any new campaign can continue from, or branch off of, with entirely different characters and an entirely different story.

## What Uro is

- A **Python core library** (`uro-core`) containing all engine logic — embeddable in any consumer.
- A **thin headless server** (`uro-server`, FastAPI) exposing the engine as a REST + WebSocket API.
- A **CLI reference client** (`uro-cli`) to play, test, and debug against the engine — the only "frontend" this repo will ever contain.

Uro integrates in two postures: **GM mode**, where Uro runs the game loop itself, and **Chronicler mode**, where an external game (tactics, 4X, even real-time) keeps its own gameplay and Uro wraps it as the world-memory and consequence layer — battles happen elsewhere; the world remembers, and only what witnesses survived to tell. See [docs/00-vision.md](docs/00-vision.md).

## What Uro is not

Anything a *platform* would build: user accounts and social features, world/asset marketplaces, ratings, forums, campaign publishing, graphical world-builder UIs, content moderation policy. The engine's job is to expose primitives (portable world/campaign formats, stable IDs, a queryable state API, exports) that make all of those buildable by consumers.

| git | GitHub / GitLab | | Uro | future platforms & games |
|---|---|---|---|---|
| repository | hosted repos | | world + timeline | hosted worlds |
| branch / fork | pull requests, forks UI | | campaign branches | "play this world's aftermath" |
| `git archive` | releases, packages | | world/campaign export packs | sharing libraries, marketplaces |
| hooks | CI/CD | | dry-run mode, event stream (outbox) | creator tooling, moderation |

## Status

**All five PoC phases plus five post-PoC phases are code-complete** (282 tests green, fully deterministic in CI), and the **core thesis was validated live** (with caveats): in an ablation run the full engine built durable state and re-surfaced an established NPC + world facts past the recency window while the `--bare` (raw-transcript) arm built nothing and drifted. For the honest map of what is *proven* vs *proxy* vs *stub-only* vs *deferred*, see [docs/16-honesty-ledger.md](docs/16-honesty-ledger.md).

What runs today, phase by phase (each with a passing acceptance test):

- **P1 — state engine:** the beat pipeline (structured + semantic recall → narration → extraction → validation gauntlet → commit → projections) with a claim/belief epistemic layer (an NPC can lie without corrupting world truth), pgvector semantic memory, four provider adapters (stub / OpenAI-compatible / Anthropic, per-role routing).
- **P2 — branching timelines:** markers, snapshots, copy-on-fork, materialize-at-any-commit, per-branch PC binding, time-skip — the **meteor test** (one event log; continue / new-life / what-if forks coexist, no special-casing).
- **P3 — mechanics:** a pluggable, deterministic ruleset port + Uro Basic (d20, seeded RNG, encounter mode); a fight replays from its seed — the **insult→combat→consequences** acceptance.
- **P4 — worlds:** the world-pack format + sufficiency check + AI backfill, import + procedural History seeding (seed 42 ≠ 43 on identical geography), prompt-template packs, capability probes.
- **P5 — server & federation:** a WS play server (broadcast, token auth), export/import with hash-chain verification, off-screen belief/rumor propagation, and Chronicler mode — the **war-story test** (an external battle's feat ripples to witnesses; a tavern NPC retells a traceable rumor).
- **P6 — the alien ruleset:** a deliberately non-d20 second built-in (`uro_pbta`: 2d6, harm clock, moves) through the *same* port — proving the ruleset port is genuinely game-agnostic (OQ-13 → D-30).
- **P7 — multiplayer:** per-participant PCs + a round-robin `PartyArbiter` behind the `TurnArbiter` port (OQ-7 → D-31).
- **P8 — Chronicler hardening:** `distill_outcome` is now trust-scoped — an external bundle can't kill/loot/first-hand-witness a PC or a T2+ actor, loot needs real ownership, replays are idempotent (OQ-12 → D-32).
- **P9 — the reaction layer:** pack-authored reactive behavior as *declarative data* (`rules.yaml` / `agendas.yaml`), never code — so a from-scratch pack sandbox is met structurally (a closed grammar that can't name a mechanical/lethal/canon event, D-33).
- **P10 — the computation layer:** engine-owned, event-sourced integer counters that **fork by construction** (so pack-authored numeric state rides `fork_branch` instead of leaking into shadow game code, D-34).

Then four games were built **on** the engine as forcing functions, producing an evidence-backed backlog ([docs/18-gap-findings.md](docs/18-gap-findings.md)) — which drove the last round of fixes and, just as usefully, *validated deferrals* (what **not** to build).

The live-model legs (thesis, planner, backfill/probe, and the post-PoC phases) are run separately with an API key — CI never makes live LLM calls. Reproduce: [docs/live-run.md](docs/live-run.md) / `scripts/ablation.sh` (thesis) + `scripts/postpoc_validate.sh` (P6/P8). Honest status: [docs/16-honesty-ledger.md](docs/16-honesty-ledger.md); horizon: [docs/10-roadmap.md](docs/10-roadmap.md).

## Quickstart

Prerequisites: Python 3.12+, [`uv`](https://docs.astral.sh/uv/), Docker, and [`just`](https://github.com/casey/just) (optional).

```sh
uv sync --all-packages          # install the workspace
docker compose up -d --wait     # Postgres 17 + pgvector (host port 5433)
uv run uro db migrate           # apply migrations
just test                       # lint + types + import-linter + tests (needs the DB up)

uv run uro world new "Ashfall"  # prints a campaign id
uv run uro play <campaign>      # play offline with the deterministic stub…
uv run uro play <campaign> --provider anthropic   # …or a real model (needs ANTHROPIC_API_KEY)

uv run python examples/hello_uro/hello_uro.py   # embed the engine as a library (no CLI, no key)
```

**Building on the engine?** [`examples/hello_uro/hello_uro.py`](examples/hello_uro/hello_uro.py) is the smallest real consumer — it imports `uro_core` directly and drives one campaign showing recall, the Reaction Layer, and branching, deterministically (no API key). Per-role model bindings go in `uro.toml` (`[llm.roles]`, see [`uro.example.toml`](uro.example.toml)); secrets stay in env vars.

## Architecture

Hexagonal by construction — the **core ring** (`domain`, `timeline`, `engines`, `pipeline`, `memory`, `session`, `chronicler`, `export`) imports only *ports*, never a concrete adapter. The rule isn't a convention; it's a CI failure (import-linter).

```
packages/
  uro-core/        all engine logic, embeddable — domain events, the beat pipeline,
                   ruleset port + two built-ins (d20 + PbtA), the Postgres adapter,
                   provider adapters, semantic memory, export/import, the reaction layer
  uro-server/      thin FastAPI shell — transport, sessions, auth, wiring (no engine logic)
  uro-cli/         the `uro` reference client — play, dry-run, world/branch/campaign tools
examples/          hello_uro (the reference embedding consumer) + four games built ON the engine
worlds/            example world packs (ashfall, thornwood, emberfell)
docs/              the authoritative design — when code and docs disagree, one is fixed to match
```

**Everything is an event.** State changes are typed domain events; projections are rebuildable read-models written only by the projector, in the same transaction as the commit. Migrations are forward-only. The extractor is fenced by a schema + an emitter whitelist, so an LLM cannot mint mechanical, lethal, or protected-canon events. See [docs/01-architecture.md](docs/01-architecture.md).

## Tests

`just test` is the gate: `ruff` + `ruff format --check` + `mypy` (strict on the core ring) + `import-linter` (the hexagonal contract) + `pytest`. **282 tests, fully deterministic** — no live LLM calls in CI (provider-dependent paths use recorded/mock transports or the deterministic stub). Postgres must be up; DB-requiring tests auto-skip if it isn't. Each phase ships a passing acceptance test (the meteor test, insult→combat→consequences, the war-story ripple, the alien-ruleset partial success, …).

## Documentation map

**Reading order:** `00`–`11` are the design (read in order); `12`–`15` are the developer-facing contracts and guides (read before writing code); `glossary.md` pins the vocabulary; `decisions.md` records why things are the way they are. The fastest way to grok the whole system is [15-walkthroughs.md](docs/15-walkthroughs.md).

| Doc | Contents |
|---|---|
| [00-vision.md](docs/00-vision.md) | Vision, scope, the engine/platform boundary, personas |
| [01-architecture.md](docs/01-architecture.md) | Package layout, subsystem map, ports & adapters |
| [02-domain-model.md](docs/02-domain-model.md) | Entities, epistemic claims/beliefs, NPC promotion tiers *(living doc)* |
| [03-timeline-and-branching.md](docs/03-timeline-and-branching.md) | Event sourcing, snapshots, branches, the meteor test |
| [04-llm-integration.md](docs/04-llm-integration.md) | Provider ports, auth (API key / Ollama / OpenAI-compatible), model roles, capability probes |
| [05-generation-pipeline.md](docs/05-generation-pipeline.md) | Beat-based pipeline, modes (free-roam vs combat), promotion rules, dry-run sandbox *(living doc)* |
| [06-rulesets.md](docs/06-rulesets.md) | Pluggable ruleset interface + the built-in "Uro Basic" d20 ruleset |
| [07-persistence-and-events.md](docs/07-persistence-and-events.md) | Postgres-first storage, pgvector, edge tables, event bus, export packs |
| [08-api-and-sessions.md](docs/08-api-and-sessions.md) | Server API, session model (single-player now, multiplayer-shaped) |
| [09-world-definition.md](docs/09-world-definition.md) | World pack format, lore import + sufficiency check, prompt template packs |
| [10-roadmap.md](docs/10-roadmap.md) | Solo-dev PoC phases with acceptance tests |
| [11-open-questions.md](docs/11-open-questions.md) | The brainstorm backlog — deliberately unresolved design areas |
| [12-event-catalog.md](docs/12-event-catalog.md) | Canonical domain event types, payloads, emitter whitelist *(v0, living)* |
| [13-contracts.md](docs/13-contracts.md) | BeatState, stage protocol, planner/extractor schemas, template contexts, failure semantics *(v0, living)* |
| [14-development-guide.md](docs/14-development-guide.md) | Setup, tooling, migrations, testing strategy, conventions, definition of done |
| [15-walkthroughs.md](docs/15-walkthroughs.md) | End-to-end traces: life of a world, life of a beat, a Chronicler-mode war story, doc responsibility map |
| [16-honesty-ledger.md](docs/16-honesty-ledger.md) | The honest proven / proxy / stub-only / deferred map |
| [17-reaction-layer.md](docs/17-reaction-layer.md) · [18-gap-findings.md](docs/18-gap-findings.md) · [19-computation-tier.md](docs/19-computation-tier.md) | Post-PoC designs: the reaction layer, the games' gap backlog, the computation tier |
| [glossary.md](docs/glossary.md) · [decisions.md](docs/decisions.md) | Every term (one line, one authority) · the ADR-style decision log |

## Contributing

Contributions and questions are welcome. Start with:

- **[CONTRIBUTING.md](CONTRIBUTING.md)** — how to set up, build, test, and submit changes, plus the project invariants (hexagonal boundary, everything-is-an-event, forward-only migrations).
- **[CLAUDE.md](CLAUDE.md)** — the working conventions and the build rhythm this repo follows.
- **[docs/14-development-guide.md](docs/14-development-guide.md)** — the full developer guide.
- **[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)** and **[SECURITY.md](SECURITY.md)**.

## Roadmap

Uro is a **proof of concept**: it proves the thesis (versioned, forkable AI-world state, validated end-to-end) rather than shipping a product. The horizon lives in [docs/10-roadmap.md](docs/10-roadmap.md); the evidence-backed backlog of what real consumers actually hit is [docs/18-gap-findings.md](docs/18-gap-findings.md). Named deferrals (the full Chronicler ingestion contract, additional rulesets, a graph/vector store swap, NATS distribution) are honest about *why* they're not built yet.

## License

Released under the [MIT License](LICENSE). © 2026 cupskeee.

## Provenance

The project began from an LLM deep-research report that described a consumer *platform*. These docs re-scope it to the *engine* and fold in the owner's feedback; they are the single source of truth (`docs/` is authoritative — when code and docs disagree, one is fixed to match the other in the same commit).
