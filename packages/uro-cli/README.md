# uro-cli

**The Uro Engine reference client — play, dry-run, and dev tools against the engine.**

`uro-cli` installs the `uro` command: create worlds, play campaigns (offline with a
deterministic stub or against a real model), fork timelines, run mechanics, and drive every
engine subsystem from the terminal. It is the reference consumer of
[`uro-core`](https://pypi.org/project/uro-core/) — the only "frontend" the project ships.

## Install

```sh
pip install uro-cli
```

This pulls `uro-core` with its `postgres` and `llm` extras — the client wires the Postgres
store and the LLM providers for you.

## Quickstart

Uro's only store is **PostgreSQL 17 + pgvector**. Start it (the reference setup uses Docker on
host port 5433), then:

```sh
docker compose up -d --wait     # from the project repository
uro db migrate
uro world new "Ashfall"         # prints a campaign id
uro play <campaign>             # offline, deterministic stub
uro play <campaign> --provider anthropic   # or a real model (needs ANTHROPIC_API_KEY)
```

If the database isn't reachable, `uro` tells you exactly how to start it. Point at a different
database with `URO_DATABASE_URL`.

## Links

- Source and docs: https://github.com/cupskeee/uro
- License: MIT
