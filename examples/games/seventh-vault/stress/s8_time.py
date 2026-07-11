"""S8 — no game<->world time mapping: the heist runs in TURNS, Uro's world runs in DAYS, and
nothing connects them. This probe measures (1) that a whole played night advances world time by
zero, (2) the by-hand mapping the game had to invent, and (3) the agenda cadence semantics that
made the epilogue tick fragile (fires ONCE per skip crossing a boundary, however many
boundaries the skip crosses).
"""

from __future__ import annotations

import sys
from pathlib import Path

_GAME_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_GAME_DIR))  # the game modules (host, client, world, frictionlog, ...)
sys.path.insert(0, str(_GAME_DIR / "stress"))  # common (the rig)

import asyncio  # noqa: E402

import host  # noqa: E402
from common import Evidence  # noqa: E402
from frictionlog import gap  # noqa: E402


async def main() -> None:
    ev = Evidence("s8_time")
    store = await host.connect_store()
    try:
        # a micro-world with one unconditioned 7-day gossip agenda
        pack = {
            "rules_api_version": 1,
            "agendas": [
                {
                    "id": "gossip-drift",
                    "every_days": 7,
                    "then": [
                        {
                            "do": "record_rumor",
                            "text": "They say the taverns get louder every week.",
                            "subjects": ["a:mera"],
                        }
                    ],
                    "scope": {"faction": "f:street"},
                }
            ],
        }
        from uro_core.domain.events import actor_created, edge_added, faction_created

        world = await store.create_world(
            "s8-time-probe",
            rule_pack=pack,
            extra_events=[
                faction_created(faction_id="f:street", name="The Street"),
                actor_created(actor_id="a:mera", name="Mera", tier=1),
                edge_added(src="a:mera", rel_type="member_of", dst="f:street"),
            ],
        )
        branch = world.main_branch_id
        campaign = await store.start_campaign(
            world.world_id, branch, participant_id="player-1", new_pc_name="Clockwatcher"
        )
        engine = host.host_engine(store)

        # (1) beats do not move the clock: a whole heist night is day-0 forever
        for i in range(3):
            await engine.run_beat(campaign, "player-1", f"I spend an hour on obstacle {i}.")
        day = await store.current_world_time(branch)
        ev.log(
            f"(1) after 3 played beats ('hours' in the fiction): world day = {day} — beats "
            "NEVER advance world time under ANY provider: BeatPlan.time_cost is parsed "
            "(plan.py:55) and even requested from the planner (plan.py:91) but consumed "
            "NOWHERE in the pipeline; the only TimeAdvanced emitter is store.time_skip. The "
            "whole heist night is a zero-duration instant, stub or live"
        )

        # (2) the invented mapping, exercised the way the arc does it
        rumors = len(await store.claims_about(branch, "a:mera"))
        await engine.agenda_tick(branch, 7)
        after_week = len(await store.claims_about(branch, "a:mera"))
        ev.log(
            f"(2) the game's mapping is BY HAND: 'the heist = one night = 0 days; epilogue "
            f"= agenda_tick(branch, 7)'. After that hand-tick: day="
            f"{await store.current_world_time(branch)}, rumors {rumors} -> {after_week}"
        )

        # (3) cadence semantics: a 14-day skip over a 7-day agenda fires ONCE, not twice
        before = len(await store.claims_about(branch, "a:mera"))
        await engine.agenda_tick(branch, 14)
        after = len(await store.claims_about(branch, "a:mera"))
        ev.log(
            f"(3) agenda_tick(14) over an every_days=7 agenda: rumors {before} -> {after} "
            f"(+{after - before}) — ONE firing for two crossed boundaries "
            "(engines/rules.py evaluate_agendas: `to_day // every <= from_day // every`); "
            "one 14-day epilogue is NOT two 7-day weeks"
        )
        assert after - before == 1, (before, after)
        # and a skip that crosses no boundary fires nothing
        await engine.agenda_tick(branch, 2)
        none_added = len(await store.claims_about(branch, "a:mera"))
        ev.log(f"    agenda_tick(2) (day 21 -> 23, no boundary): rumors stay {none_added}")
        assert none_added == after, (none_added, after)

        gap(
            gap="A game<->world time mapping (heist turns/rounds -> world days) so downtime "
            "and the Chronicler share a clock",
            happened="Beats NEVER advance world time — BeatPlan.time_cost is a dead field the "
            "planner is prompted to emit (parsed at plan.py:55, consumed nowhere; the only "
            "TimeAdvanced emitter is store.time_skip); OutcomeBundle.duration_rounds is "
            "decorative (chronicler.py — no time semantics); the epilogue only happens because "
            "arc.py calls agenda_tick(7) by hand, and a 14-day skip fires a 7-day agenda once, "
            "not twice",
            workaround="The game DECLARES 'one heist = one night = day 0; epilogue = +7 days' "
            "in arc.py and owns the tick",
            severity="annoyance",
            needs="a campaign-declared clock policy (e.g. beats-per-day, or bundle "
            "duration_rounds -> hours) + per-boundary agenda evaluation (or documented "
            "once-per-skip semantics)",
            evidence="stress/s8_time.py (all three measurements); engines/rules.py "
            "evaluate_agendas; arc.py agenda_tick call",
        )
        ev.log(
            "VERDICT: hit. The deferral is livable for a one-night heist (the game owns the "
            "clock in two lines), but any campaign where fiction time EMERGES from play "
            "(seasons, travel, decay) would have to shadow the entire calendar in game code."
        )
    finally:
        await store.close()
    ev.flush()


if __name__ == "__main__":
    asyncio.run(main())
