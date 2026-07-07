# Uro Engine

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

**Phase 0 + Phase 1 are code-complete** (72 tests), and the **core thesis is validated live**: in an ablation run, the full engine's narrator recalled a named NPC and a plot fact ~11 beats later — past the recency window — while the `--bare` (raw-transcript) arm forgot them entirely. What runs today: the full beat pipeline — structured + semantic recall → narration → extraction → validation gauntlet → commit → projections — with a claim/belief epistemic layer (an NPC can lie without corrupting world truth), pgvector semantic memory, four LLM provider adapters (stub, OpenAI-compatible, Anthropic, with per-role routing), and a thesis harness (`--bare` ablation + `uro consistency`). Reproduce it: [docs/live-run.md](docs/live-run.md) / `scripts/ablation.sh` (needs an API key). Signature feature next: **branching timelines** (Phase 2). See [10-roadmap.md](docs/10-roadmap.md).

## Quickstart

Prerequisites: Python 3.12+, [`uv`](https://docs.astral.sh/uv/), Docker, and [`just`](https://github.com/casey/just) (optional).

```sh
uv sync --all-packages          # install the workspace
docker compose up -d --wait     # Postgres + pgvector (host port 5433)
uv run uro db migrate           # apply migrations
just test                       # lint + types + import-linter + tests (needs the DB up)

uv run uro world new "Ashfall"  # prints a campaign id
uv run uro play <campaign>      # play offline with the deterministic stub…
uv run uro play <campaign> --provider anthropic   # …or a real model (needs ANTHROPIC_API_KEY)
```

Per-role model bindings go in `uro.toml` (`[llm.roles]`, see `uro.example.toml`); secrets stay in env vars. Contributing conventions and the build rhythm live in [CLAUDE.md](CLAUDE.md); the developer guide is [14-development-guide.md](docs/14-development-guide.md).

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
| [glossary.md](docs/glossary.md) | Every term, one line, one authority |
| [decisions.md](docs/decisions.md) | ADR-style log of every decision made so far, with rationale |

## Provenance

This documentation supersedes `deep-research-report.md` (an LLM deep-research output kept for reference). The report described a consumer *platform*; these docs re-scope the project to the *engine* and fold in the owner's feedback. Where the report and these docs disagree, these docs win.
