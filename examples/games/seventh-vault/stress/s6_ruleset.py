"""S6 — one ruleset per server process: the stealth phase and the loud phase arguably want
different mechanics (a PbtA-style partial-success ladder for infiltration, d20 for the fight).
Attempt to want that mid-heist and capture what the wire says.
"""

from __future__ import annotations

import sys
from pathlib import Path

_GAME_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_GAME_DIR))  # the game modules (host, client, world, frictionlog, ...)
sys.path.insert(0, str(_GAME_DIR / "stress"))  # common (the rig)

import asyncio  # noqa: E402

from client import CrewClient  # noqa: E402
from common import Evidence, rig_up  # noqa: E402
from frictionlog import gap  # noqa: E402
from world import CREW  # noqa: E402


async def main() -> None:
    ev = Evidence("s6_ruleset")
    # the running server binds uro-basic (the arc's ruleset)
    rig = await rig_up("s6-ruleset", n_clients=0)
    store = rig.store
    try:
        # a second world for the "loud phase", bound to uro-pbta — same server process
        world = await store.create_world(  # type: ignore[attr-defined]
            "Seventh Vault — loud phase", ruleset_id="uro-pbta"
        )
        campaign = await store.start_campaign(  # type: ignore[attr-defined]
            world.world_id,
            world.main_branch_id,
            participant_id="player-1",
            new_pc_name="Vesna-under-fire",
            ruleset_id="uro-pbta",
        )
        ev.log(
            f"created a second campaign {campaign.campaign_id} bound to uro-pbta "
            "(the library allows it — rulesets are per-campaign in the data model)"
        )
        c = CrewClient(
            base=rig.server.base,
            campaign_id=campaign.campaign_id,
            token=CREW[0][1],
            participant_id="player-1",
            role="cracksman",
        )
        await c.connect()
        try:
            await c.wait_for("participant_joined", where={"participant_id": "player-1"})
            await c.send_intent("I kick the door and let the dice go loud.")
            frame = await c.wait_for("beat_failed", timeout=30)
            ev.log(f"the SAME server refused to play it: beat_failed error = {frame['error']!r}")
        finally:
            await c.close()
        gap(
            gap="Switch mechanics for the loud phase (uro-pbta partial-success infiltration vs "
            "d20 combat) on one server",
            happened="The server binds ONE ruleset per process; a campaign pinned to another "
            "gets a clean beat_failed ('…is bound to ruleset uro-pbta, but this server runs "
            "uro-basic…', uro_server/app.py:59-64) — honest, but a second heist phase needs a "
            "second server process and a client-side handoff",
            workaround="The heist stayed on uro-basic throughout — which cost nothing, because "
            "under the stub provider NO ruleset's mechanics are reachable anyway (the sharper "
            "S6 finding: ruleset choice was moot before the one-per-process limit could bite)",
            severity="annoyance",
            needs="per-campaign engine binding server-side (D-30 rebind, deferred) — but only "
            "after the planner path works without a live LLM at all",
            evidence="stress/s6_ruleset.py; uro_server/app.py:53-64; uro_cli/main.py:487-492",
        )
        ev.log(
            "VERDICT: the limit is real and cleanly enforced, but it did NOT constrain THIS "
            "game — the stub planner resolves zero checks under ANY ruleset, so the deeper "
            "constraint (S7/G: mechanics unreachable deterministically) made the ruleset "
            "choice cosmetic. The deferral was the right call; fix planner reachability first."
        )
    finally:
        await rig.close()
    ev.flush()


if __name__ == "__main__":
    asyncio.run(main())
