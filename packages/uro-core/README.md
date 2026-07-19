# uro-core

**The Uro Engine core library — the embeddable, headless world-state engine.**

Uro is a *world-state engine*: the persistent, versioned, forkable canonical record of a
living fictional world. A model narrates; a fenced extractor distills that narration into a
queryable world model — actors, places, factions, beliefs, open threads — that stays
authoritative while the story plays over it. It tracks not only what is *true* but *who knows
it*, and world state is **versioned and forkable**: a finished campaign's ending becomes a
point in history any new campaign can continue from, branch off, or explore as a what-if.

`uro-core` contains all engine logic and is embeddable in any consumer — the engine only ever
sees ports (hexagonal architecture), never concrete adapters.

## Install

```sh
pip install "uro-core[postgres,llm]"
```

The base install is the pure engine (it imports only ports). The shipped adapters live behind
extras:

- `postgres` — the bundled event store + semantic memory over PostgreSQL + pgvector.
- `llm` — the bundled LLM provider adapters (OpenAI-compatible + Anthropic).
- `all` — both.

An embedder can install the base package and supply their own adapters behind the same ports.

## Runtime

The bundled store requires **PostgreSQL 17 + pgvector**. The reference setup runs it via
Docker on host port 5433; see the project repository.

## Links

- Source, docs, and the reference CLI/server: https://github.com/cupskeee/uro
- License: MIT
