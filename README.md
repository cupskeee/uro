<p align="center">
  <img src="docs/images/URO_Engine_logo.png" alt="Uro Engine" width="460">
</p>

<p align="center"><b>Worlds that remember. Timelines that fork.</b></p>

<p align="center">
  <a href="https://pypi.org/project/uro-cli/"><img src="https://img.shields.io/pypi/v/uro-cli?label=pip%20install%20uro-cli&color=blue" alt="PyPI"></a>
  <a href="https://github.com/cupskeee/uro/actions/workflows/ci.yml"><img src="https://github.com/cupskeee/uro/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/Python-3.12-blue" alt="Python 3.12">
  <img src="https://img.shields.io/badge/status-proof--of--concept-orange" alt="Status: proof of concept">
</p>

**Uro is a world-state engine for AI games.** Think an event-sourced save-game with Git-style branching for your world's canon — and an LLM game master writing scenes on top of it. Every change is a typed event, so an NPC can lie, a rumor can spread, or a character can simply be *wrong* — **without corrupting what's actually true**, because the narrator physically cannot mint mechanical facts (damage, death, terrain) it only described. Fork the timeline and both realities persist from one log: *a city razed on one branch stays a crater there while it still stands on another.*

> **Honest status:** a single-developer **proof of concept** (Python 3.12 · Postgres + pgvector · MIT · [`v0.2.0` on PyPI](https://pypi.org/project/uro-cli/)). The deterministic core is real and CI-proven — **359 tests, no LLM calls needed to verify them.** The LLM-driven parts are validated in specific, documented runs with named caveats. Full map: [the honesty ledger](docs/16-honesty-ledger.md).

## Two ways to use it

**🎲 GM mode — Uro runs the story.** You send a player's intent; Uro recalls the relevant world state, an LLM narrates the scene, a fenced extractor distills that prose back into canon, and it commits. For AI-driven RPGs, roguelikes, and interactive fiction where the model *is* the game master.

**📜 Chronicler mode — your game runs the story; Uro remembers it.** Already have your own combat and turns? Keep them. You report what happened — who fought, who died, who *saw* it — and Uro tracks what's true, who believes what, and how a rumor garbles as it travels to those who weren't there. **No rewrite of your game loop, no LLM, and no API key on this path.**

## Quickstart

```sh
pip install uro-cli

# Uro's store is Postgres + pgvector (required — there is no in-memory mode yet):
docker run -d --name uro-pg -p 5433:5432 \
  -e POSTGRES_DB=uro -e POSTGRES_USER=uro -e POSTGRES_PASSWORD=uro \
  pgvector/pgvector:pg17

uro db migrate
uro world new "Ashfall"        # prints a campaign id
uro play <campaign-id>         # play offline with the deterministic stub…
uro play <campaign-id> --provider anthropic   # …or a real model (needs ANTHROPIC_API_KEY)
```

If the database isn't reachable, `uro` tells you exactly how to start it. Point elsewhere with `URO_DATABASE_URL`.

## Embed it in your game — GM mode

`uro-core` is a plain library; drive a campaign from your own code. (The base install is the pure engine; add the bundled adapters with `pip install "uro-core[postgres,llm]"`.)

```python
import asyncio
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.pipeline.engine import Engine
from uro_core.providers.router import ProviderRouter

# `provider` is any object with async stream()/complete()/embed() —
# uro_core.providers.adapters.openai_compat / anthropic, or your own (e.g. for tests).

async def main() -> None:
    store = PostgresEventStore("postgresql://uro:uro@localhost:5433/uro")
    await store.connect()
    await store.migrate()

    engine = Engine(store, ProviderRouter(bindings={}, default=provider))

    world = await store.create_world("Saltmarsh", tone=["noir", "damp", "wary"])
    campaign = await store.start_campaign(
        world.world_id, world.main_branch_id,
        participant_id="player-1", new_pc_name="the Inspector",
    )

    result = await engine.run_beat(
        campaign, "player-1", "I ask the innkeeper about the missing dockworker.",
    )
    print(result.narration)   # the scene the model wrote
    print(result.commit_id)   # …now committed to the world's timeline

    await store.close()

asyncio.run(main())
```

[`examples/hello_uro/hello_uro.py`](examples/hello_uro/hello_uro.py) is the runnable version of this — **no API key needed** (a scripted provider stands in for the LLM), showing recall, the reaction layer, and branching in one script.

## Bolt it onto your game — Chronicler mode

Your game resolved a fight its own way. Report the outcome; Uro turns it into world memory and propagates the story to survivors:

```python
from uro_core.chronicler import Feat, LootTransfer, OutcomeBundle, distill_outcome

bundle = OutcomeBundle(
    encounter_id="e:salt-road",
    participants=["a:hero", "a:champion", "a:raider"],
    witnesses=["a:raider"],          # survivors who saw it
    casualties=["a:champion"],       # a rank-and-file combatant actually dies
    feats=[Feat(actor="a:hero",
                description="a lone wizard split the warband's champion in two")],
    loot=[LootTransfer(item_id="i:champion-blade", from_ref="a:champion", to_ref="a:hero")],
)

events = await distill_outcome(store, campaign.branch_id, bundle)  # deterministic — no LLM, no key
await engine.append_and_react(campaign, events)                   # commit + fire pack reaction rules
```

The feat propagates along who-knows-whom edges — an eyewitness recounts it firsthand, someone two hops away retells a garbled, low-confidence rumor, and with zero survivors it's never told at all. Two things worth knowing up front (both by design):

- **The outcome path is a deliberate trust boundary.** A reported death of a **player character or a named, higher-tier actor downgrades to an unconfirmed rumor** rather than real canon death — so a self-reported battle can't assassinate your story's king by accident. Only rank-and-file combatants actually die.
- **You get a receipt back.** `distill_outcome_with_receipt` (and the outcome HTTP endpoint) report a per-reference disposition — `applied` / `downgraded` / `dropped` — so you know exactly what became canon versus rumor. Roster, state, and chronicle are also readable over the authed REST surface; a few ops (export/import, branches, usage) stay CLI-only for now — see [what "proof of concept" means](#what-proof-of-concept-means-for-you-today).

A full worked example is [`examples/games/ironwake`](examples/games/ironwake) (a tactics-style game integrated this way, with its own gap report); the integration contract is [`examples/games/URO_INTEGRATION.md`](examples/games/URO_INTEGRATION.md).

## What you get

- **Forkable timelines, by construction.** Branch the whole world from any point and both lines diverge legitimately from one event log — continue a campaign, start a fresh one generations later, or explore a what-if. (The "meteor test", [`test_meteor.py`](packages/uro-core/tests/test_meteor.py).)
- **Truth vs. belief.** Canon and what-characters-believe are separate layers; the extractor's schema *cannot express* mechanical outcomes, so hallucinated prose can't rewrite reality. NPCs can hold false beliefs and lie.
- **Pluggable rulesets.** Two structurally different systems ship through one interface — a d20 ruleset (hp/AC) *and* a PbtA-style one (2d6, harm clocks, moves). Not hardcoded to any one game; a seeded fight replays byte-for-byte.
- **A declarative reaction layer.** Packs ship reactive behavior as **data** (`rules.yaml` / `agendas.yaml`), never code — the grammar structurally can't name a lethal or canon-minting event, so pack authors are sandboxed by construction.
- **Engine-owned counters** that fork, replay, and export with the rest of the world; **multiplayer** (per-player PCs behind a turn-arbiter port with round-robin / proposal / vote shapes); **export/import** with a tamper-evident (re-derived, keyless) hash chain.
- **Bring your own model.** A deterministic stub for tests, plus OpenAI-compatible (incl. local Ollama) and Anthropic adapters, bindable per role.

## What "proof of concept" means for you today

Uro is honest about being a PoC. Concretely, before you build on it:

- **Postgres + pgvector is required** from the first command — there's no in-memory/SQLite mode.
- **Some management is CLI-only.** The server exposes an authed REST surface (worlds, campaigns, roster, state, chronicle, join, time-skip, tokens) plus the WebSocket play channel and the outcome endpoint — but seed, branches, export/import, probes, and usage stay CLI-only for now.
- **One ruleset per server process** (the embedding/CLI paths rebind per campaign; the server doesn't yet).
- **The LLM-driven legs need your own key.** The thesis ablation and live alien-ruleset play are validated only in specific documented runs; world-pack AI backfill and capability probes are stub-tested only (never run live). The deterministic core needs no model to verify. `uro consistency` is a labeled *proxy* metric, not a full verification pass.

The full proven / proxy / stub-only / deferred breakdown lives in [`docs/16-honesty-ledger.md`](docs/16-honesty-ledger.md).

## Learn more

- **[docs/](docs/README.md)** — the design docs, the build log, the honesty ledger, and the full documentation map.
- **[examples/](examples/)** — [`hello_uro`](examples/hello_uro/hello_uro.py) (the reference embedding) + four games built *on* the engine as forcing functions.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — set up the workspace, run the test gate, and the project invariants. (For contributors, the dev path is `git clone` + `uv sync --all-packages` + `just test`.)

## License

[MIT](LICENSE) © 2026 cupskeee.
