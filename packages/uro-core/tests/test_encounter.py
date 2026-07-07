"""Phase 3 acceptance: encounter mode (docs/06, 10). Deterministic — no LLM.

The roadmap acceptance: a free-roam insult escalates into combat (the pipeline decides the
mode transition), a multi-round fight resolves under Uro Basic, and a LOST fight leaves
persistent consequences — an injury claim and a looted item — visible in later free-roam.
Asserted on committed events/projections, not prose.
"""

from collections.abc import AsyncIterator

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import actor_created, item_created, sheet_updated
from uro_core.domain.ids import new_id
from uro_core.pipeline.encounter import run_encounter
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets.base import CharSpec, Combatant
from uro_core.rulesets.rng import Rng
from uro_core.rulesets.uro_basic import UroBasic

RS = UroBasic()


def _sheet(abilities: dict[str, int], tier: int = 1) -> dict:
    return RS.new_character(CharSpec(abilities=abilities, weapon_tier=tier), Rng(0)).model_dump()


# --- pure encounter runner: deterministic replay + effect events ---


def test_run_encounter_replays_identically_and_emits_effects() -> None:
    pc = Combatant(actor_id="a:pc", team="party", sheet=RS.new_character(CharSpec(), Rng(0)))
    foe = Combatant(
        actor_id="a:foe",
        team="foes",
        sheet=RS.new_character(CharSpec(abilities={"CON": 8}), Rng(0)),
    )
    e1, o1 = run_encounter(RS, [pc, foe], Rng(42), encounter_id="e:1")
    e2, o2 = run_encounter(RS, [pc, foe], Rng(42), encounter_id="e:1")
    assert [ev.payload for ev in e1] == [ev.payload for ev in e2]  # byte-identical replay
    assert o1.model_dump() == o2.model_dump()
    assert o1.winner_team in ("party", "foes")
    assert e1[0].event_type == "EncounterStarted" and e1[-1].event_type == "EncounterEnded"
    assert any(ev.event_type == "EncounterTurnTaken" for ev in e1)
    assert any(ev.event_type == "ActorDamaged" for ev in e1)  # a hit landed → damage event
    # a different seed produces a different fight
    e3, _ = run_encounter(RS, [pc, foe], Rng(7), encounter_id="e:1")
    assert [ev.payload for ev in e3] != [ev.payload for ev in e1]


# --- the acceptance: insult → combat → lost-fight consequences ---


class _Scripted:
    """Serves a canned plan for the planner + fixed narration; extractor gets nothing."""

    def __init__(self, *, plan_json: str, narration: str) -> None:
        self._plan = plan_json
        self._narration = narration

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield self._narration

    async def complete(self, req: CompletionRequest) -> str:
        return self._plan if req.stage_tag == "planner" else '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


def _engine(store: PostgresEventStore, plan: str, narration: str) -> Engine:
    provider = _Scripted(plan_json=plan, narration=narration)
    return Engine(store, ProviderRouter(bindings={}, default=provider), ruleset=RS)


async def test_the_acceptance_insult_to_combat_to_consequences(store: PostgresEventStore) -> None:
    world = await store.create_world(f"Ashfall-{new_id()}")
    main = world.main_branch_id
    # A weak PC (Bram): high CON for a little staying power, but AC 7 and feeble attacks — he
    # cannot dent the brute and will be worn down (a multi-round, deterministic loss).
    campaign = await store.start_campaign(
        world.world_id,
        main,
        participant_id="player-2",
        new_pc_name="Bram",
        new_pc_id="a:bram",
        pc_sheet=_sheet({"STR": 8, "DEX": 4, "CON": 18}),
        ruleset_id="uro-basic",
    )
    # Grull the brute + his sheet (STR 20, tanky), and Bram's walking-stick (loot).
    await store.append_beat(
        main,
        [
            actor_created(actor_id="a:grull", name="Grull", tier=2, role="brute"),
            sheet_updated(
                actor_id="a:grull",
                sheet=_sheet({"STR": 20, "DEX": 14, "CON": 20}),
                ruleset_id="uro-basic",
            ),
            item_created(item_id="i:stick", name="worn walking-stick", owner_ref="a:bram"),
        ],
    )

    # (1) a free-roam insult — no mechanics, mode stays freeroam
    insult = _engine(
        store, '{"intent_class":"dialogue","triggers":[],"mechanics":[]}', "Bram jeers at Grull."
    )
    r1 = await insult.run_beat(campaign, "player-2", "I insult Grull the brute")
    assert r1.commit_id  # committed as an ordinary beat

    # (2) the escalation: Bram swings → the plan invokes the encounter-starting affordance
    attack_plan = (
        '{"intent_class":"action","triggers":["violence"],'
        '"mechanics":[{"affordance":"attack","actor":"a:bram","target":"a:grull"}]}'
    )
    combat = _engine(store, attack_plan, "Steel and knuckles — but Grull is a wall.")
    r2 = await combat.run_beat(campaign, "player-2", "I swing at Grull")

    # the fight resolved and Bram LOST
    assert (await store.get_sheet(main, "a:bram"))["hp"] == 0  # Bram is down (injured)
    assert (await store.get_sheet(main, "a:grull"))["hp"] > 0  # Grull stands
    injuries = [c for c in await store.claims_about(main, "a:bram") if c.truth == "true"]
    assert any("wounded" in c.statement for c in injuries)  # the injury is canon
    assert (await store.get_item(main, "i:stick"))["owner_ref"] == "a:grull"  # looted

    # the beat carried the whole fight: a mode transition, an initiative loop, an ending
    async with store.pool.acquire() as conn:
        types = [
            r["event_type"]
            for r in await conn.fetch(
                "SELECT event_type FROM events WHERE commit_id = $1 ORDER BY seq", r2.commit_id
            )
        ]
    assert types.count("ModeChanged") == 2  # freeroam→encounter→freeroam
    assert "EncounterStarted" in types and "EncounterEnded" in types
    assert types.count("EncounterTurnTaken") >= 2  # a multi-round fight, not a one-shot
    assert "ItemTransferred" in types and types[-1] == "BeatResolved"

    # (3) consequences persist into later free-roam
    after = _engine(
        store, '{"intent_class":"examine","triggers":[],"mechanics":[]}', "Bram nurses his ribs."
    )
    await after.run_beat(campaign, "player-2", "I check my wounds")
    assert (await store.get_sheet(main, "a:bram"))["hp"] == 0  # still down a beat later
    assert (await store.get_item(main, "i:stick"))["owner_ref"] == "a:grull"  # still looted

    # (4) and a fork carries the fallout — the snapshot 'sheets'/'items' sections + replay
    head = await store.get_branch(main)
    fork = await store.fork_branch(world.world_id, head.head_commit, "aftermath")
    assert (await store.get_sheet(fork.branch_id, "a:bram"))["hp"] == 0  # injured on the fork too
    assert (await store.get_item(fork.branch_id, "i:stick"))["owner_ref"] == "a:grull"  # looted


async def test_pc_wins_and_loots_the_foe(store: PostgresEventStore) -> None:
    # The other consequence branch: a strong PC downs a weak foe and takes its loot.
    world = await store.create_world(f"test-{new_id()}")
    main = world.main_branch_id
    campaign = await store.start_campaign(
        world.world_id,
        main,
        participant_id="player-1",
        new_pc_name="Hero",
        new_pc_id="a:hero",
        pc_sheet=_sheet({"STR": 20, "DEX": 14, "CON": 20}),
        ruleset_id="uro-basic",
    )
    await store.append_beat(
        main,
        [
            actor_created(actor_id="a:goblin", name="Goblin", tier=1),
            sheet_updated(
                actor_id="a:goblin",
                sheet=_sheet({"STR": 8, "DEX": 6, "CON": 6}),
                ruleset_id="uro-basic",
            ),
            item_created(item_id="i:coin", name="copper coin", owner_ref="a:goblin"),
        ],
    )
    plan = (
        '{"intent_class":"action","triggers":["violence"],'
        '"mechanics":[{"affordance":"attack","actor":"a:hero","target":"a:goblin"}]}'
    )
    await _engine(store, plan, "The Goblin never stood a chance.").run_beat(
        campaign, "player-1", "I strike the Goblin"
    )
    assert (await store.get_sheet(main, "a:goblin"))["hp"] == 0  # goblin down
    assert (await store.get_sheet(main, "a:hero"))["hp"] > 0  # hero stands
    assert (await store.get_item(main, "i:coin"))["owner_ref"] == "a:hero"  # hero looted it
    assert any("wounded" in c.statement for c in await store.claims_about(main, "a:goblin"))


async def test_attack_with_no_valid_target_falls_back_to_freeroam(
    store: PostgresEventStore,
) -> None:
    # Review fix: an attack with no distinct, known opponent must NOT fabricate a won encounter.
    world = await store.create_world(f"test-{new_id()}")
    main = world.main_branch_id
    campaign = await store.start_campaign(
        world.world_id,
        main,
        participant_id="player-1",
        new_pc_name="Solo",
        new_pc_id="a:solo",
        pc_sheet=_sheet({"STR": 12}),
        ruleset_id="uro-basic",
    )
    plan = (
        '{"intent_class":"action","triggers":["violence"],'
        '"mechanics":[{"affordance":"attack","actor":"a:solo","target":""}]}'
    )
    result = await _engine(store, plan, "Solo swings at empty air.").run_beat(
        campaign, "player-1", "I swing wildly"
    )
    async with store.pool.acquire() as conn:
        types = [
            r["event_type"]
            for r in await conn.fetch(
                "SELECT event_type FROM events WHERE commit_id = $1", result.commit_id
            )
        ]
    assert "EncounterStarted" not in types and "ModeChanged" not in types  # no fabricated fight
    assert "BeatResolved" in types  # committed as an ordinary free-roam beat


async def test_combat_events_project_hp_and_ownership(store: PostgresEventStore) -> None:
    # Direct check of the effect→projection wiring the acceptance rides on.
    from uro_core.domain.events import actor_damaged, item_transferred

    world = await store.create_world(f"test-{new_id()}")
    main = world.main_branch_id
    await store.append_beat(
        main,
        [
            actor_created(actor_id="a:x", name="X"),
            sheet_updated(actor_id="a:x", sheet=_sheet({"CON": 12}), ruleset_id="uro-basic"),
            item_created(item_id="i:1", name="ring", owner_ref="a:x"),
        ],
    )
    start_hp = (await store.get_sheet(main, "a:x"))["hp"]
    await store.append_beat(main, [actor_damaged(actor_id="a:x", amount=3, source="a:y")])
    assert (await store.get_sheet(main, "a:x"))["hp"] == start_hp - 3  # damage reduced hp
    await store.append_beat(main, [actor_damaged(actor_id="a:x", amount=999, source="a:y")])
    assert (await store.get_sheet(main, "a:x"))["hp"] == 0  # clamped at 0, never negative
    await store.append_beat(main, [item_transferred(item_id="i:1", from_ref="a:x", to_ref="a:y")])
    assert (await store.get_item(main, "i:1"))["owner_ref"] == "a:y"  # ownership moved
