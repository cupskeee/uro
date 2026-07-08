#!/usr/bin/env python
"""Seed a Chronicler war-story so a LIVE narrator can be asked to retell the rumor (Phase 8, D-32).

Deterministic (NO api key): seeds a rumor-mill world, runs the toy external battle, distills the
OutcomeBundle through the (now trust-scoped) Chronicler path, and starts a campaign with a PC — then
prints the campaign id. `scripts/postpoc_validate.sh` pipes a "what have you heard?" intent to
`uro play <that id> --provider openai`, so a REAL narrator retells the low-confidence rumor. The
point of the live leg: does the garbled, third-hand belief actually surface as a HEDGED rumor in
real prose (confidence → certainty phrasing), not as settled fact? Analyze from Postgres after.

    uv run python scripts/warstory_live.py            # prints: CAMPAIGN <id>  BRANCH <id>
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from toy_battler import fight
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.chronicler import distill_outcome
from uro_core.domain.events import actor_created, edge_added
from uro_core.domain.ids import new_id

DSN = "postgresql://uro:uro@localhost:5433/uro"


async def _main() -> None:
    store = PostgresEventStore(DSN)
    await store.connect()
    try:
        await store.migrate()
        world = await store.create_world(f"warstory-live-{new_id()}")
        branch = world.main_branch_id
        # PC-less cast + a rumor path: raider1 -knows-> townsfolk -knows-> Mera (tavern keeper).
        await store.append_beat(
            branch,
            [
                actor_created(actor_id="a:hero", name="Sable the wizard", tier=2),
                actor_created(actor_id="a:champion", name="The warband champion", tier=1),
                actor_created(actor_id="a:raider1", name="A scarred raider", tier=1),
                actor_created(actor_id="a:raider2", name="A young raider", tier=1),
                actor_created(actor_id="a:townsfolk", name="A road pedlar", tier=1),
                actor_created(actor_id="a:mera", name="Mera", tier=1, role="tavern keeper"),
                edge_added(src="a:raider1", rel_type="knows", dst="a:townsfolk"),
                edge_added(src="a:townsfolk", rel_type="knows", dst="a:mera"),
            ],
        )
        # the EXTERNAL game resolves the battle; one raider survives to witness the feat
        outcome = fight("a:hero", ["a:champion", "a:raider1", "a:raider2"], seed=7, survivors=1)
        await store.append_beat(branch, await distill_outcome(store, branch, outcome))

        # a campaign with a traveler PC, so `uro play` can walk in and ask Mera what she has heard
        campaign = await store.start_campaign(
            world.world_id,
            branch,
            participant_id="player-1",
            new_pc_name="Traveler",
            new_pc_id="a:traveler",
        )
        print(f"CAMPAIGN {campaign.campaign_id}  BRANCH {branch}")
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(_main())
