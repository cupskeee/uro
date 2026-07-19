# uro-server

**The Uro Engine headless server — a thin FastAPI shell over [`uro-core`](https://pypi.org/project/uro-core/).**

`uro-server` exposes the engine as a REST + WebSocket API. It is transport and session wiring
only — no engine logic. It runs the WebSocket play channel (broadcast fan-out, token auth) and
the Chronicler outcome endpoint over a testable dependency seam.

## Install

```sh
pip install uro-server
```

## Runtime

The server drives the engine, which requires **PostgreSQL 17 + pgvector** and (for live play) an
LLM provider — both pulled via `uro-core`'s extras. The reference way to run a server is
`uro serve` from [`uro-cli`](https://pypi.org/project/uro-cli/), which builds the dependencies.

## Links

- Source and docs (see `docs/08-api-and-sessions.md`): https://github.com/cupskeee/uro
- License: MIT
