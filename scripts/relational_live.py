#!/usr/bin/env python
"""A scripted live session whose story names FACTIONS and PLACES (the D-50/D-51 relational path).

The two post-D-50 live sessions never named a group or placed anyone at a named location, so the
relational extractor (factions, `member_of`/`located_in` affiliations, edges) never fired. This
plays a fixed list of intents that steer the narrator toward a walled town, the order that keeps
its peace, a rival group, their people, and a shared trouble — plus deliberate pressure to restate
what's already known (the same trouble asked about three ways, a report repeated to a knight).

Mirrors `uro play` (same Engine, the campaign's own ruleset so the planner runs) but takes its
models from the model-connection registry (D-47), as `uro serve` does. Each beat completes before
the next starts. Prints each beat's narration + what it committed, then a summary; analyze further
from Postgres (the world is named in the output).

    uv run python scripts/relational_live.py               # registry models (keyed)
    uv run python scripts/relational_live.py --provider stub   # dry-run the driver
    uv run python scripts/relational_live.py --scenario resolution   # a plot set up, then settled

The `resolution` scenario (D-52) sets up a plot and then settles it, to check that the extractor
resolves the thread when — and only when — the story actually ends it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotenv import find_dotenv, load_dotenv
from extraction_replay import DSN, _label, _summary
from uro_cli.main import PARTICIPANT, _build_pc_sheet
from uro_cli.wiring import build_ruleset
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.ids import new_id
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import StubProvider
from uro_core.providers.registry import build_router_from_registry
from uro_core.providers.router import ProviderRouter

INTENTS = [
    "I walk up the muddy road to the walled river town ahead and ask the gate guard its name.",
    "I ask the guard whose colors he wears — which order or house keeps the peace here.",
    "I ask where that order's knights are garrisoned in town.",
    "I head into the busiest tavern I can find and ask the barkeep the tavern's name.",
    "I ask the barkeep who the hooded strangers in the corner are and which group they belong to.",
    "I ask the barkeep how the order and those strangers get along.",
    "I ask the barkeep what trouble has the whole town on edge lately.",
    "I ask the barkeep to tell me more about that trouble — who is behind it?",
    "I ask an old patron at the bar what he thinks about that same trouble.",
    "I walk over to the hooded strangers, introduce myself, and ask their leader's name.",
    "I ask their leader what their group wants in this town.",
    "I leave the tavern and walk to the order's garrison.",
    "I ask the knight at the garrison gate for his name and rank.",
    "I tell the knight what the hooded strangers told me.",
    "I ask the knight what the order plans to do about the trouble.",
]

RESOLUTION = [
    "I walk into the village of Millbrook and ask the innkeeper why everyone looks so worried.",
    "I ask the innkeeper to tell me everything about the miller's missing daughter.",
    "I promise to find her and ask where she was last seen.",
    "I go to the old mill at the edge of the village to look for clues.",
    "I search the mill loft carefully, calling out her name.",
    "I follow the trail I find and keep searching until I find her.",
    "I ask the girl what happened and whether she is hurt.",
    "I bring the girl home to her father, the miller.",
    "I tell the innkeeper the miller's daughter is safe and home.",
]

SCENARIOS = {"relational": INTENTS, "resolution": RESOLUTION}


async def _main(provider: str, scenario: str) -> None:
    load_dotenv(find_dotenv(usecwd=True))  # URO_SECRET_KEY decrypts the registry's credentials
    store = PostgresEventStore(DSN)
    await store.connect()
    try:
        if provider == "stub":
            router = ProviderRouter({}, default=StubProvider())
        else:
            built = await build_router_from_registry(store)
            if built is None:
                raise SystemExit("the model-connection registry is empty (see `uro provider`)")
            router = built
        world = await store.create_world(f"{scenario}-live-{new_id()}")
        rs = build_ruleset()  # the default ruleset, as `uro world new` uses
        sheet, ruleset_id = _build_pc_sheet(rs), rs.id
        campaign = await store.start_campaign(
            world.world_id,
            world.main_branch_id,
            participant_id=PARTICIPANT,
            new_pc_name="Adventurer",
            pc_sheet=sheet,
            starting_items=["a traveler's knife"],
            ruleset_id=ruleset_id,
        )
        engine = Engine(
            store, router, ruleset=build_ruleset(campaign.ruleset_id, campaign.ruleset_version)
        )
        print(f"world {world.name!r}  campaign {campaign.campaign_id}  ({provider})\n")

        for i, intent in enumerate(SCENARIOS[scenario], 1):
            narration = "".join(
                [c async for c in engine.run_beat_stream(campaign, PARTICIPANT, intent)]
            )
            async with store.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT e.event_type AS type, e.payload FROM branches b JOIN events e "
                    "ON e.commit_id = b.head_commit WHERE b.branch_id = $1 ORDER BY e.seq",
                    campaign.branch_id,
                )
            shown = [
                _label(dict(r))
                for r in rows
                if r["type"] not in ("BeatResolved", "BeliefChanged", "SheetUpdated")
            ]
            print(f"--- beat {i}: {intent}\n{narration.strip()}")
            print(f"  committed: {' || '.join(shown) or '(nothing)'}\n")

        summary = await _summary(store, world.world_id)
        print("\n".join(f"{k:24}{v:>6}" for k, v in summary.items()))
    finally:
        await store.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--provider", choices=["registry", "stub"], default="registry")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="relational")
    args = ap.parse_args()
    asyncio.run(_main(args.provider, args.scenario))
