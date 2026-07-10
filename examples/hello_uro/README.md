# examples

Reference programs built **on** the Uro engine — how a game or app embeds it.

## `hello_uro.py` — the smallest real consumer

Imports `uro_core` directly (no `uro` CLI, no server) and drives one campaign that shows the three
signature capabilities end to end:

1. **State-tracked recall** — a fact established in an early beat re-surfaces later as known continuity.
2. **The Reaction Layer** (D-33) — pack-authored declarative rules react to committed state: a
   downtime tick wakes a dormant plot and spreads a rumor, and it reaches the narrator.
3. **Branching timelines** — from one event log, a "continue" line and a "what-if" fork diverge.

It's **deterministic**: a scripted provider stands in for the LLM, so there's no API key and the
output is byte-stable (which is why `test_example_hello_uro.py` asserts the whole arc in CI). Swap
`ScriptedProvider` for `uro_cli.wiring.build_provider("openai", ...)` and the same code narrates live.

```
docker compose up -d --wait          # Postgres + pgvector on host port 5433
uv run uro db migrate
uv run python examples/hello_uro/hello_uro.py
```
