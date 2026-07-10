"""hello_uro — the smallest real program built ON the Uro engine (docs/01, the git:GitHub thesis).

This is the "how do I embed Uro?" reference. It imports `uro_core` DIRECTLY — no `uro` CLI, no
server — and drives a full campaign the way a game/app would, showing the three signature
capabilities end to end:

  1. STATE-TRACKED RECALL (the thesis)  — the engine extracts durable facts from narration and
     re-surfaces them as known continuity a later beat can lean on.
  2. THE REACTION LAYER (D-33)          — pack-authored declarative rules react to committed state:
     a downtime tick wakes a dormant plot and spreads a rumor, and that reaches the narrator.
  3. BRANCHING TIMELINES (the meteor)   — from ONE event log, a "continue" line and a "what-if"
     fork coexist and legitimately diverge.

It is DETERMINISTIC: a scripted provider stands in for the LLM (so there is no API key and the
output is byte-stable), which is also why `test_example_hello_uro.py` can assert the whole arc in
CI. Swap `ScriptedProvider` for a real one (`uro_cli.wiring.build_provider("openai", ...)`) and the
same code narrates live.

Run it against a local Postgres (docker compose up -d; host port 5433):

    uv run python examples/hello_uro/hello_uro.py
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import (
    actor_created,
    edge_added,
    faction_created,
    thread_created,
)
from uro_core.pipeline.engine import Engine
from uro_core.pipeline.recall import assemble_recall
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter

DSN = "postgresql://uro:uro@localhost:5433/uro"


# --- 1. A provider is just three async methods (docs/04). Here it serves canned narration +
# extraction per beat so the demo is deterministic; a real provider calls a model instead. ---


class ScriptedProvider:
    """Serves queued (narration, extraction-JSON) pairs in beat order. `embed` uses the stub
    hashing embedder so semantic recall works with no external service."""

    def __init__(self, beats: list[tuple[str, str]]) -> None:
        self._narrations = [n for n, _ in beats]
        self._extractions = [x for _, x in beats]
        self._n = self._x = 0

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        text = self._narrations[self._n]
        self._n += 1
        yield text

    async def complete(self, req: CompletionRequest) -> str:  # the extractor call
        x = self._extractions[self._x]
        self._x += 1
        return x

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


# The world's Reaction-Layer rules (docs/17) — DATA, not code. Two downtime agendas that fire on a
# 30-day cadence: the feud plot escalates, and a rumor spreads among the dockside faction. Each
# rule's `scope` is its jurisdiction (the gauntlet drops anything it tries to touch outside it).
RULE_PACK = {
    "rules_api_version": 1,
    "agendas": [
        {
            "id": "feud-escalates",
            "every_days": 30,
            "when": {"kind": "thread_state", "thread": "t:feud", "state": "dormant"},
            "then": [{"do": "set_thread_state", "thread": "t:feud", "to": "active"}],
            "scope": {"thread": "t:feud"},
        },
        {
            "id": "rumor-spreads",
            "every_days": 30,
            "then": [
                {
                    "do": "record_rumor",
                    "text": "They say the smugglers and the dockhands are at open war now.",
                    "subjects": ["a:mera"],
                }
            ],
            "scope": {"faction": "f:dockside"},  # a:mera is a member → in jurisdiction
        },
    ],
}

# The scripted beats: (narration the "model" returns, the extraction JSON the extractor returns).
# Beat 1 establishes Mera + a durable fact; beats 2-3 wander (nothing durable) — so beat 4 proves
# the fact is RECALLED, not merely still on screen.
BEATS = [
    (
        "I settle at the Rusty Anchor and ask Mera about the missing dockworker.",
        "Mera the innkeeper sets down a chipped mug and lowers her voice. 'Aye — young Pell "
        "vanished off the north dock last week. The smugglers know something, mark me.'",
        '{"actors":[{"name":"Mera","role":"innkeeper"}],'
        '"claims":[{"statement":"Mera runs the Rusty Anchor and blames the smugglers for the '
        'dock disappearances.","about":["Mera"],"provenance":"narrator"}]}',
    ),
    (
        "I step out and walk the misty waterfront.",
        "Fog rolls off the black water; rigging creaks against the dark.",
        '{"actors":[],"claims":[]}',
    ),
    (
        "I warm my hands at a brazier by the pier.",
        "The coals hiss; a gull complains somewhere overhead.",
        '{"actors":[],"claims":[]}',
    ),
]


async def demo(store: PostgresEventStore) -> dict[str, Any]:
    """Drive the whole arc and return the observable results (what the CI test asserts on)."""
    provider = ScriptedProvider([(n, x) for _, n, x in BEATS])
    # no ruleset bound → the Phase-1 recall/narrate/extract flow (no planner/mechanics gate)
    engine = Engine(store, ProviderRouter(bindings={}, default=provider))

    # --- Create a world (the pack's authored geography/cast + its reaction rules, carried inline
    # so an exported world stays self-contained), then start a campaign with a fresh PC. ---
    world = await store.create_world(
        "Saltmarsh",
        tone=["noir", "damp", "wary"],
        rule_pack=RULE_PACK,
        extra_events=[
            faction_created(faction_id="f:dockside", name="The Dockside Union"),
            actor_created(actor_id="a:mera", name="Mera", tier=2, role="innkeeper"),
            edge_added(src="a:mera", rel_type="member_of", dst="f:dockside"),
            thread_created(
                thread_id="t:feud", stakes="Smugglers vs. the dockhands.", state="dormant"
            ),
        ],
    )
    branch = world.main_branch_id
    campaign = await store.start_campaign(
        world.world_id, branch, participant_id="player-1", new_pc_name="the Inspector"
    )

    # --- Play the beats. Each returns a BeatResult (commit id + how many facts were extracted). ---
    last_commit = ""
    for intent, _, _ in BEATS:
        result = await engine.run_beat(campaign, "player-1", intent)
        last_commit = result.commit_id

    # (1) RECALL: a later beat's context re-surfaces the established fact about Mera.
    recall = await assemble_recall(
        store, branch, "I find Mera again and press her on the smugglers", 8
    )
    recalled_fact = next(
        (c.statement for c in recall.claims if "smugglers" in c.statement.lower()), ""
    )

    # (3) BRANCH: fork a "what-if" from BEFORE downtime (the feud never escalated here).
    whatif = await store.fork_branch(world.world_id, last_commit, "what-if-calm")

    # (2) REACTION: on the MAIN line, a month passes. The downtime agenda tick wakes the feud and
    # spreads a rumor — pack rules reacting to committed state, no LLM involved.
    await engine.agenda_tick(branch, 30)

    main_threads = {t.thread_id: t.state for t in await store.list_threads(branch)}
    whatif_threads = {t.thread_id: t.state for t in await store.list_threads(whatif.branch_id)}
    mera_claims = await store.claims_about(branch, "a:mera")
    rumors = [c.statement for c in mera_claims if c.truth == "unknown"]
    # the woken plot now reaches the narrator (recall surfaces active threads, docs/04)
    post = await assemble_recall(store, branch, "what is stirring in Saltmarsh?", 8)
    active_plots = [t.stakes for t in post.active_threads]

    return {
        "world_id": world.world_id,
        "beats_played": len(BEATS),
        "recalled_fact": recalled_fact,
        "main_feud_state": main_threads.get("t:feud"),
        "whatif_feud_state": whatif_threads.get("t:feud"),
        "module_rumors": rumors,
        "active_plots_seen_by_narrator": active_plots,
    }


async def _main() -> None:
    store = PostgresEventStore(DSN)
    await store.connect()
    try:
        await store.migrate()
        r = await demo(store)
        print("=== hello_uro — a campaign built on the Uro engine ===\n")
        print(f"world {r['world_id']} · {r['beats_played']} beats played\n")
        print("1. RECALL — a fact established early, re-surfaced later as known continuity:")
        print(f"     “{r['recalled_fact']}”\n")
        print("2. REACTION LAYER — after a month's downtime, pack rules reacted (no LLM):")
        print(f"     the feud thread: dormant → {r['main_feud_state']}")
        for rumor in r["module_rumors"]:
            print(f"     a rumor spread:  “{rumor}”")
        plots = r["active_plots_seen_by_narrator"]
        print(f"     the narrator now sees these active plots: {plots}\n")
        print("3. BRANCHING — one event log, two coexisting lines:")
        print(f"     main (continue): the feud is {r['main_feud_state']}")
        print(f"     what-if (forked before downtime): the feud is {r['whatif_feud_state']}")
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(_main())
