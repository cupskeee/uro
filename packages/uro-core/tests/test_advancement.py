"""Fight and downtime hooks (D-63): the engine says WHEN (a fight ended, time passed), each ruleset
says WHAT (a d20 level for winning, a milestone for surviving, XP for a missed move, rest), and the
world's settings say HOW MUCH. Before D-63 none of this was ever applied.
"""

from collections.abc import AsyncIterator

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import actor_created, sheet_updated
from uro_core.domain.ids import new_id
from uro_core.pipeline.encounter import run_encounter
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets import registry
from uro_core.rulesets.base import CharSpec, Combatant, EncounterOutcome
from uro_core.rulesets.rng import Rng


def _sheet(rid: str, **data: object) -> dict:  # type: ignore[type-arg]
    return registry.resolve(rid).new_character(CharSpec(data=data), Rng(0))


def _fight(rid: str, config: dict | None, pc: dict, foe: dict, seed: int = 7):  # type: ignore[no-untyped-def,type-arg]
    rs = registry.resolve(rid, "", config)
    fighters = [
        Combatant(actor_id="a:pc", team="party", sheet=pc, is_pc=True),
        Combatant(actor_id="a:foe", team="foes", sheet=foe),
    ]
    events, outcome = run_encounter(rs, fighters, Rng(seed), encounter_id="e1")
    final = {
        e.payload["actor_id"]: e.payload["sheet"] for e in events if e.event_type == "SheetUpdated"
    }
    return outcome, final


def test_a_d20_pc_levels_up_for_winning() -> None:
    strong = _sheet("uro-basic", abilities={"STR": 18, "CON": 16, "DEX": 14}, weapon_tier=3)
    weak = _sheet("uro-basic", abilities={"STR": 6, "CON": 6, "DEX": 6})
    outcome, final = _fight("uro-basic", None, strong, weak)
    assert outcome.winner_team == "party"
    assert final["a:pc"]["level"] == 2 and final["a:pc"]["hp"] == final["a:pc"]["max_hp"]
    assert final["a:foe"]["level"] == 1  # the loser, and not a PC
    _, off = _fight("uro-basic", {"levels_per_win": 0}, strong, weak)
    assert off["a:pc"]["level"] == 1
    _, no_heal = _fight("uro-basic", {"heal_on_level": False}, strong, weak)
    assert no_heal["a:pc"]["level"] == 2  # still levels; hp stays where the fight left it


def test_a_npc_winner_never_advances() -> None:
    rs = registry.resolve("uro-basic")
    strong = _sheet("uro-basic", abilities={"STR": 18, "CON": 16}, weapon_tier=3)
    state = rs.start_encounter(
        __import__("uro_core.rulesets.base", fromlist=["EncounterCtx"]).EncounterCtx(
            encounter_id="e", combatants=[Combatant(actor_id="a:npc", team="foes", sheet=strong)]
        ),
        Rng(0),
    )
    won = EncounterOutcome(winner_team="foes", survivors=["a:npc"])
    assert rs.conclude_encounter(state, won, frozenset())["a:npc"]["level"] == 1


def test_a_pool_pc_earns_a_milestone_for_surviving_and_rests_in_downtime() -> None:
    strong = _sheet("uro-pool", traits={"might": 5, "nerve": 5})
    weak = _sheet("uro-pool", traits={"might": 1, "nerve": 1}, wounds=2)
    outcome, final = _fight("uro-pool", None, strong, weak)
    assert "a:pc" in outcome.survivors and final["a:pc"]["milestones"] == 1
    _, three = _fight("uro-pool", {"milestones_per_fight": 3}, strong, weak)
    assert three["a:pc"]["milestones"] == 0  # three milestones = one advance, spent at once
    assert three["a:pc"]["traits"]["wits"] == strong["traits"]["wits"] + 1  # the weakest trait

    pool = registry.resolve("uro-pool")
    strained = {**strong, "strain": 4}
    assert pool.downtime(strained, 1)["strain"] == 0  # type: ignore[index]
    assert pool.downtime(strong, 1) is None  # nothing to clear
    slow = registry.resolve("uro-pool", "", {"rest_days": 3})
    assert slow.downtime(strained, 2) is None and slow.downtime(strained, 3) is not None
    assert registry.resolve("uro-pool", "", {"rest_days": 0}).downtime(strained, 99) is None


def test_a_pbta_pc_marks_xp_for_each_missed_move_in_a_fight() -> None:
    # dice scripted low so every move misses
    class _Low(Rng):
        def roll(self, n: int, sides: int) -> int:
            return 2

    rs = registry.resolve("uro-pbta")
    pc = _sheet("uro-pbta")
    fighters = [
        Combatant(actor_id="a:pc", team="party", sheet=pc, is_pc=True),
        Combatant(actor_id="a:foe", team="foes", sheet=_sheet("uro-pbta")),
    ]
    events, _ = run_encounter(rs, fighters, _Low(0), encounter_id="e1")
    misses = sum(
        1
        for e in events
        if e.event_type == "EncounterTurnTaken"
        and e.payload["actor_id"] == "a:pc"
        and e.payload["result"] == "miss"
    )
    final = {
        e.payload["actor_id"]: e.payload["sheet"] for e in events if e.event_type == "SheetUpdated"
    }
    assert misses >= 1
    total = pc["xp"] + misses  # through progression: a full track of 5 spends into a stat
    assert final["a:pc"]["xp"] == total % 5
    assert final["a:foe"]["xp"] == 0  # not a PC
    assert rs.downtime(pc, 30) is None


class _Stub:
    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "x"

    async def complete(self, req: CompletionRequest) -> str:
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


async def test_a_time_skip_rests_the_ruleset_s_own_sheets(store: PostgresEventStore) -> None:
    world = await store.create_world(f"Rest-{new_id()}", ruleset_id="uro-pool")
    branch = world.main_branch_id
    tired = f"a:tired-{new_id()}"
    strained = {**_sheet("uro-pool"), "strain": 3}
    other = {**_sheet("uro-basic")}
    await store.append_beat(
        branch,
        [
            actor_created(actor_id=tired, name="Tired"),
            sheet_updated(actor_id=tired, sheet=strained, ruleset_id="uro-pool"),
            actor_created(actor_id="a:old", name="Old"),
            sheet_updated(actor_id="a:old", sheet=other, ruleset_id="uro-basic"),
        ],
    )
    pool = registry.resolve("uro-pool")
    await Engine(store, ProviderRouter(bindings={}, default=_Stub()), ruleset=pool).agenda_tick(
        branch, 2
    )
    assert (await store.get_sheet(branch, tired))["strain"] == 0  # type: ignore[index]
    assert await store.get_sheet(branch, "a:old") == other  # another ruleset's: not this one's
    async with store.pool.acquire() as conn:
        rested_by_ruleset = await conn.fetchval(
            "SELECT count(*) FROM events WHERE event_type = 'SheetUpdated' "
            "AND entity_refs @> ARRAY[$1] AND caused_by->>'kind' = 'ruleset'",
            tired,
        )
    assert rested_by_ruleset == 1

    # no ruleset bound → the skip happens, nothing rests
    await store.append_beat(
        branch, [sheet_updated(actor_id=tired, sheet=strained, ruleset_id="uro-pool")]
    )
    await Engine(store, ProviderRouter(bindings={}, default=_Stub())).agenda_tick(branch, 2)
    assert (await store.get_sheet(branch, tired))["strain"] == 3  # type: ignore[index]


async def test_play_marks_a_pc_as_a_pc_in_its_fights(store: PostgresEventStore) -> None:
    from uro_core.pipeline.plan import BeatPlan, PlanMechanic

    world = await store.create_world(f"Arena-{new_id()}", ruleset_id="uro-pool")
    branch = world.main_branch_id
    campaign = await store.start_campaign(
        world.world_id,
        branch,
        participant_id="p1",
        new_pc_name="Ash",
        new_pc_id=f"a:ash-{new_id()}",
        pc_sheet=_sheet("uro-pool", traits={"might": 5, "nerve": 5}),
        ruleset_id="uro-pool",
    )
    pc = await store.campaign_pc(campaign.campaign_id)
    await store.append_beat(
        branch,
        [
            actor_created(actor_id="a:bane", name="Bane", tier=1),
            sheet_updated(
                actor_id="a:bane",
                sheet=_sheet("uro-pool", traits={"might": 1, "nerve": 1}, wounds=2),
                ruleset_id="uro-pool",
            ),
        ],
    )
    engine = Engine(
        store, ProviderRouter(bindings={}, default=_Stub()), ruleset=registry.resolve("uro-pool")
    )
    plan = BeatPlan(
        intent_class="action",
        triggers=["violence"],
        mechanics=[PlanMechanic(affordance="strike", actor=pc, target="a:bane")],
    )
    await engine.run_beat(campaign, "p1", "I strike Bane", plan=plan)
    after = await store.get_sheet(branch, pc)  # type: ignore[arg-type]
    assert after is not None and after["milestones"] == 1  # the PC survived and earned it
    assert (await store.get_sheet(branch, "a:bane"))["milestones"] == 0  # type: ignore[index]


# --- phase-end review fixes ------------------------------------------------------------------


def test_a_low_cap_never_lowers_a_trait_and_awards_are_bounded() -> None:
    # Review: `min(cap, value + 1)` LOWERED a trait that started above a low cap, and an unbounded
    # award setting looped once per point (a pack could stall the server).
    from uro_core.rulesets.base import Award

    pool = registry.resolve("uro-pool", "", {"trait_cap": 1})
    sheet = _sheet("uro-pool")  # traits of 2, above the cap
    assert pool.progression(sheet, Award(data={"milestone": 3}))["traits"] == sheet["traits"]
    pbta = registry.resolve("uro-pbta", "", {"stat_cap": -1})
    stats = _sheet("uro-pbta")
    assert pbta.progression(stats, Award(data={"xp": 5}))["stats"] == stats["stats"]
    for rid, key in (("uro-pbta", "xp_on_miss"), ("uro-pool", "milestones_per_fight")):
        with pytest.raises(ValueError, match=key):
            registry.resolve(rid, "", {key: 10**12})
    many = registry.resolve("uro-pbta").progression(stats, Award(data={"xp": 10**9}))
    assert all(v == 3 for v in many["stats"].values())  # capped, computed — not 2e8 loop turns


async def test_a_check_never_lands_on_another_players_pc_or_an_npc(
    store: PostgresEventStore,
) -> None:
    # Review (P7 x D-61): a plan could name another participant's PC (or an NPC) as a check's actor,
    # so P1's beat committed strain/XP onto P2's PC. Now: another player's PC isn't rolled for, and
    # only the acting PC's sheet takes a check's consequences.
    from uro_core.pipeline.plan import BeatPlan, PlanMechanic
    from uro_core.rulesets.uro_pool import UroPool

    class _AlwaysStrains(UroPool):
        def resolve_check(self, req, rng):  # type: ignore[no-untyped-def]
            result = super().resolve_check(req, rng)
            return result.model_copy(update={"sheet": {**req.sheet, "strain": 4}})

    world = await store.create_world(f"Party-{new_id()}", ruleset_id="uro-pool")
    branch = world.main_branch_id
    campaign = await store.start_campaign(
        world.world_id,
        branch,
        participant_id="p1",
        new_pc_name="Ash",
        new_pc_id=f"a:ash-{new_id()}",
        pc_sheet=_sheet("uro-pool"),
        ruleset_id="uro-pool",
    )
    bran = await store.bind_pc(
        campaign.campaign_id,
        "p2",
        new_pc_name="Bran",
        pc_sheet=_sheet("uro-pool"),
        ruleset_id="uro-pool",
    )
    ash = await store.pc_for_participant(campaign.campaign_id, "p1")
    await store.append_beat(
        branch,
        [
            actor_created(actor_id="a:guard", name="Guard"),
            sheet_updated(actor_id="a:guard", sheet=_sheet("uro-pool"), ruleset_id="uro-pool"),
        ],
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=_Stub()), ruleset=_AlwaysStrains())
    plan = BeatPlan(
        mechanics=[
            PlanMechanic(affordance="investigate", actor=bran),
            PlanMechanic(affordance="investigate", actor="a:guard"),
            PlanMechanic(affordance="investigate", actor=ash),
        ]
    )
    result = await engine.run_beat(campaign, "p1", "Bran, the guard and I search", plan=plan)
    assert result.checks == 2  # the guard and Ash: Bran rolls in his own beats
    assert (await store.get_sheet(branch, bran))["strain"] == 0  # type: ignore[index]
    assert (await store.get_sheet(branch, "a:guard"))["strain"] == 0  # type: ignore[index]
    assert (await store.get_sheet(branch, ash))["strain"] == 4  # type: ignore[arg-type,index]
