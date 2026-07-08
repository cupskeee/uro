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
    return RS.new_character(CharSpec(data={"abilities": abilities, "weapon_tier": tier}), Rng(0))


# --- pure encounter runner: deterministic replay + effect events ---


def test_run_encounter_replays_identically_and_emits_effects() -> None:
    pc = Combatant(actor_id="a:pc", team="party", sheet=RS.new_character(CharSpec(), Rng(0)))
    foe = Combatant(
        actor_id="a:foe",
        team="foes",
        sheet=RS.new_character(CharSpec(data={"abilities": {"CON": 8}}), Rng(0)),
    )
    e1, o1 = run_encounter(RS, [pc, foe], Rng(42), encounter_id="e:1")
    e2, o2 = run_encounter(RS, [pc, foe], Rng(42), encounter_id="e:1")
    assert [ev.payload for ev in e1] == [ev.payload for ev in e2]  # byte-identical replay
    assert o1.model_dump() == o2.model_dump()
    assert o1.winner_team in ("party", "foes")
    assert e1[0].event_type == "EncounterStarted" and e1[-1].event_type == "EncounterEnded"
    assert any(ev.event_type == "EncounterTurnTaken" for ev in e1)
    # harm reaches the timeline as the ruleset's OPAQUE final sheet (D-30), not a typed hp event
    assert any(ev.event_type == "SheetUpdated" for ev in e1)
    # a different seed produces a different fight
    e3, _ = run_encounter(RS, [pc, foe], Rng(7), encounter_id="e:1")
    assert [ev.payload for ev in e3] != [ev.payload for ev in e1]


def test_acceptance_fight_pc_loses_across_seeds() -> None:
    # Backs the "deterministic across seeds" claim: with the acceptance stats the PC loses a
    # multi-round fight for EVERY seed (no draw, no turn-cap) — the outcome is seed-invariant.
    def bram() -> Combatant:
        return Combatant(
            actor_id="a:bram",
            team="party",
            sheet=RS.new_character(
                CharSpec(data={"abilities": {"STR": 8, "DEX": 4, "CON": 18}}), Rng(0)
            ),
        )

    def grull() -> Combatant:
        return Combatant(
            actor_id="a:grull",
            team="foes",
            sheet=RS.new_character(
                CharSpec(data={"abilities": {"STR": 20, "DEX": 14, "CON": 20}}), Rng(0)
            ),
        )

    for seed in range(300):
        events, outcome = run_encounter(RS, [bram(), grull()], Rng(seed), encounter_id="e")
        assert outcome.winner_team == "foes" and "a:bram" in outcome.out_of_fight
        assert sum(1 for e in events if e.event_type == "EncounterTurnTaken") >= 2  # multi-round


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


async def test_fork_from_marker_after_combat_restores_hp_and_loot(
    store: PostgresEventStore,
) -> None:
    # Exercises the snapshot 'sheets'/'items' RESTORE path (not just replay): a fork from a
    # MARKER after a fight carries the reduced hp + moved ownership out of the snapshot blob.
    # Also verifies start_campaign's live starting-item emitter (the PC owns a real item in play).
    from uro_core.domain.events import item_transferred

    world = await store.create_world(f"test-{new_id()}")
    main = world.main_branch_id
    campaign = await store.start_campaign(
        world.world_id,
        main,
        participant_id="p1",
        new_pc_name="Xan",
        new_pc_id="a:x",
        pc_sheet=_sheet({"CON": 12}),
        starting_items=["a knife"],  # a real item, created in play (not a test fixture)
        ruleset_id="uro-basic",
    )
    assert campaign.campaign_id is not None
    knife = (await store.items_owned_by(main, "a:x"))[0]  # the starting item exists

    # simulate a lost fight's persistent effects: Xan downed (opaque final sheet), knife looted
    downed = {**(await store.get_sheet(main, "a:x") or {}), "hp": 0}
    await store.append_beat(
        main,
        [
            actor_created(actor_id="a:y", name="Yorn"),
            sheet_updated(actor_id="a:x", sheet=downed, ruleset_id="uro-basic"),
            item_transferred(item_id=knife, from_ref="a:x", to_ref="a:y"),
        ],
    )
    assert (await store.get_sheet(main, "a:x"))["hp"] == 0
    assert (await store.get_item(main, knife))["owner_ref"] == "a:y"

    # marker snapshots the head; a fork FROM the marker restores state from that snapshot blob
    await store.create_marker(world.world_id, "after-fight", main)
    fork = await store.fork_branch(world.world_id, "after-fight", "replay")
    assert (await store.get_sheet(fork.branch_id, "a:x"))["hp"] == 0  # sheets section restored
    assert (await store.get_item(fork.branch_id, knife))["owner_ref"] == "a:y"  # items restored


async def test_combat_events_project_hp_and_ownership(store: PostgresEventStore) -> None:
    # Direct check of the effect→projection wiring the acceptance rides on: harm reaches the
    # projection as the ruleset's OPAQUE final sheet (SheetUpdated), never a typed hp event (D-30).
    from uro_core.domain.events import item_transferred

    world = await store.create_world(f"test-{new_id()}")
    main = world.main_branch_id
    sheet = _sheet({"CON": 12})
    await store.append_beat(
        main,
        [
            actor_created(actor_id="a:x", name="X"),
            sheet_updated(actor_id="a:x", sheet=sheet, ruleset_id="uro-basic"),
            item_created(item_id="i:1", name="ring", owner_ref="a:x"),
        ],
    )
    start_hp = (await store.get_sheet(main, "a:x"))["hp"]
    await store.append_beat(
        main,
        [
            sheet_updated(
                actor_id="a:x", sheet={**sheet, "hp": start_hp - 3}, ruleset_id="uro-basic"
            )
        ],
    )
    assert (await store.get_sheet(main, "a:x"))["hp"] == start_hp - 3  # opaque sheet replaced
    await store.append_beat(
        main, [sheet_updated(actor_id="a:x", sheet={**sheet, "hp": 0}, ruleset_id="uro-basic")]
    )
    assert (await store.get_sheet(main, "a:x"))["hp"] == 0
    await store.append_beat(main, [item_transferred(item_id="i:1", from_ref="a:x", to_ref="a:y")])
    assert (await store.get_item(main, "i:1"))["owner_ref"] == "a:y"  # ownership moved


async def test_legacy_actor_damaged_still_rebuilds_hp_on_replay(store: PostgresEventStore) -> None:
    # Phase-6 review regression: the current runner emits harm as opaque SheetUpdated, but a
    # PRE-Phase-6 d20 log recorded fight HP ONLY as accumulated ActorDamaged reductions (no closing
    # SheetUpdated). The legacy projector handler must remain so such a log still rebuilds by replay
    # (the non-negotiable "projections are rebuildable read-models" invariant) — proven via a fork.
    from uro_core.domain.events import actor_damaged

    world = await store.create_world(f"test-{new_id()}")
    main = world.main_branch_id
    sheet = _sheet({"CON": 12})
    await store.append_beat(
        main,
        [
            actor_created(actor_id="a:leg", name="Legacy"),
            sheet_updated(actor_id="a:leg", sheet=sheet, ruleset_id="uro-basic"),
        ],
    )
    start_hp = (await store.get_sheet(main, "a:leg"))["hp"]
    # a legacy per-hit ActorDamaged (as the old d20 runner emitted) still reduces hp
    await store.append_beat(main, [actor_damaged(actor_id="a:leg", amount=4, source="a:foe")])
    assert (await store.get_sheet(main, "a:leg"))["hp"] == start_hp - 4
    # and it REBUILDS by replay: a fork projects the same reduced hp from the same event log
    head = await store.get_branch(main)
    fork = await store.fork_branch(world.world_id, head.head_commit, "replay-legacy")
    assert (await store.get_sheet(fork.branch_id, "a:leg"))["hp"] == start_hp - 4


async def test_planner_target_by_name_still_forms_the_encounter(store: PostgresEventStore) -> None:
    # Live-run fix (2026-07-09): a small planner names the target ("Grull") instead of its id
    # ("a:grull"). _resolve_encounter now entity-resolves the ref, so the fight still forms
    # (previously it fell through get_actor and dropped silently to free-roam).
    world = await store.create_world(f"name-{new_id()}")
    main = world.main_branch_id
    campaign = await store.start_campaign(
        world.world_id,
        main,
        participant_id="p1",
        new_pc_name="Bram",
        new_pc_id="a:bram",
        pc_sheet=_sheet({"STR": 8, "DEX": 4, "CON": 18}),
        ruleset_id="uro-basic",
    )
    await store.append_beat(
        main,
        [
            actor_created(actor_id="a:grull", name="Grull", tier=2, role="brute"),
            sheet_updated(
                actor_id="a:grull",
                sheet=_sheet({"STR": 20, "DEX": 14, "CON": 20}),
                ruleset_id="uro-basic",
            ),
        ],
    )
    # the planner emits target="Grull" — a NAME, not "a:grull"
    plan = (
        '{"intent_class":"action","triggers":["violence"],'
        '"mechanics":[{"affordance":"attack","target":"Grull"}]}'
    )
    result = await _engine(store, plan, "Bram swings at Grull.").run_beat(
        campaign, "p1", "I attack Grull"
    )
    async with store.pool.acquire() as conn:
        types = [
            r["event_type"]
            for r in await conn.fetch(
                "SELECT event_type FROM events WHERE commit_id = $1", result.commit_id
            )
        ]
    assert "EncounterStarted" in types  # the name resolved → the fight formed
    assert (await store.get_sheet(main, "a:bram"))[
        "hp"
    ] == 0  # and it actually resolved (Bram lost)


async def test_unresolvable_target_name_falls_back_to_freeroam(store: PostgresEventStore) -> None:
    # A name that matches no known actor must NOT fabricate a fight — it falls back to free-roam.
    world = await store.create_world(f"noname-{new_id()}")
    main = world.main_branch_id
    campaign = await store.start_campaign(
        world.world_id,
        main,
        participant_id="p1",
        new_pc_name="Solo",
        new_pc_id="a:solo",
        pc_sheet=_sheet({"STR": 12}),
        ruleset_id="uro-basic",
    )
    plan = (
        '{"intent_class":"action","triggers":["violence"],'
        '"mechanics":[{"affordance":"attack","target":"Nobody"}]}'
    )
    result = await _engine(store, plan, "Solo swings at a shadow.").run_beat(
        campaign, "p1", "I attack Nobody"
    )
    async with store.pool.acquire() as conn:
        types = [
            r["event_type"]
            for r in await conn.fetch(
                "SELECT event_type FROM events WHERE commit_id = $1", result.commit_id
            )
        ]
    assert "EncounterStarted" not in types and "BeatResolved" in types  # no fabricated fight
