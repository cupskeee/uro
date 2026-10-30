"""Uro Pool (D-55): the third built-in ruleset — an original d6 dice-pool system. Deterministic.

It earns its place by stressing the port on axes neither earlier ruleset touches: target-number
pools, a push that costs strain, wounds that shrink every pool, and SIMULTANEOUS exchanges (an
action produces no effect until the round closes). Rules are unit-tested with scripted dice; the
port is proven by the real encounter runner and a full fight through the engine pipeline.
"""

from collections.abc import AsyncIterator

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import actor_created, sheet_updated
from uro_core.domain.ids import new_id
from uro_core.pipeline.encounter import run_encounter
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets import registry
from uro_core.rulesets.base import Action, Award, CharSpec, CheckRequest, Combatant, EncounterCtx
from uro_core.rulesets.rng import Rng
from uro_core.rulesets.uro_pool import Sheet, UroPool

POOL = UroPool()


class _Dice(Rng):
    """Scripted d6 faces, in order — so a test states exactly what was rolled."""

    def __init__(self, *faces: int) -> None:
        super().__init__(0)
        self._faces = list(faces)

    def die(self, sides: int) -> int:
        return self._faces.pop(0)


def _sheet(**traits: int) -> dict:  # type: ignore[type-arg]
    return POOL.new_character(CharSpec(data={"traits": traits}), Rng(0))


def test_new_character_has_its_own_shape() -> None:
    sheet = _sheet(might=3)
    assert sheet["traits"] == {"might": 3, "wits": 2, "nerve": 2}
    assert {"wounds", "strain", "milestones"} <= sheet.keys()
    assert not {"hp", "ac", "harm", "stats"} & sheet.keys()  # neither d20 nor PbtA


def test_difficulty_sets_the_target_and_marks_grade_the_outcome() -> None:
    req = CheckRequest(sheet=_sheet(wits=3), stat="wits", difficulty="hard")  # 5+
    fail = POOL.resolve_check(
        req.model_copy(update={"sheet": {**_sheet(wits=3), "strain": 5}}), _Dice(4, 4, 1)
    )
    assert fail.outcome == "fail" and fail.detail["target"] == 5  # 4s don't mark at 5+
    assert POOL.resolve_check(req, _Dice(5, 1, 1)).outcome == "success"
    assert POOL.resolve_check(req, _Dice(5, 6, 6)).outcome == "triumph"  # 3 marks
    easy = req.model_copy(update={"difficulty": "easy"})  # 3+
    assert POOL.resolve_check(easy, _Dice(3, 1, 1)).outcome == "success"


def test_a_failed_roll_is_pushed_once_while_strain_allows() -> None:
    req = CheckRequest(sheet=_sheet(nerve=2), stat="nerve")
    pushed = POOL.resolve_check(req, _Dice(1, 2, 6, 1))  # fail → reroll → one mark
    assert pushed.outcome == "success" and pushed.detail["pushed"] is True
    spent = req.model_copy(update={"sheet": {**_sheet(nerve=2), "strain": 5}})
    no_push = POOL.resolve_check(spent, _Dice(1, 2))  # strain at the limit → no reroll
    assert no_push.outcome == "fail" and no_push.detail["pushed"] is False


def test_wounds_shrink_every_pool_but_never_below_one_die() -> None:
    s = Sheet(traits={"might": 3, "wits": 1, "nerve": 2}, wounds=2)
    assert (s.pool("might"), s.pool("wits"), s.pool("nerve")) == (1, 1, 1)


def test_milestones_advance_the_weakest_trait_and_rest_clears_strain() -> None:
    sheet = {**_sheet(might=3, wits=1), "strain": 4, "milestones": 2}
    after = POOL.progression(sheet, Award(data={"milestone": 1, "rest": True}))
    assert after["traits"]["wits"] == 2 and after["milestones"] == 0 and after["strain"] == 0


def test_an_exchange_resolves_only_when_every_fighter_has_committed() -> None:
    # The simultaneous turn model the runner must host: commit, commit, THEN everything happens.
    ctx = EncounterCtx(
        encounter_id="e1",
        combatants=[
            Combatant(actor_id="a:ash", team="party", sheet=_sheet(might=2, nerve=1)),
            Combatant(actor_id="a:bane", team="foes", sheet=_sheet(might=2, nerve=1)),
        ],
    )
    state = POOL.start_encounter(ctx, Rng(0))
    assert POOL.current_actor(state) == "a:ash"
    strike_bane = Action(kind="strike", actor_id="a:ash", target_id="a:bane")
    state, effects = POOL.resolve_action(state, strike_bane, Rng(0))
    assert (
        effects == [] and POOL.current_actor(state) == "a:bane"
    )  # committed, nothing happened yet
    strike_ash = Action(kind="strike", actor_id="a:bane", target_id="a:ash")
    # ash: attack [6,6] vs bane's defence [1] → 2 wounds; bane: attack [5,1] vs ash's [2] → 1 wound
    state, effects = POOL.resolve_action(state, strike_ash, _Dice(6, 6, 1, 5, 1, 2))
    sheets = POOL.sheets(state)
    assert (sheets["a:bane"]["wounds"], sheets["a:ash"]["wounds"]) == (2, 1)  # mutual, same round
    assert [e.kind for e in effects] == ["wound", "wound"]
    assert POOL.current_actor(state) == "a:ash"  # a new round begins


def test_an_encounter_terminates_and_replays_identically_under_the_real_runner() -> None:
    fighters = [
        Combatant(actor_id="a:ash", team="party", sheet=_sheet(might=4, nerve=3)),
        Combatant(actor_id="a:bane", team="foes", sheet=_sheet(might=2, nerve=1)),
        Combatant(actor_id="a:crow", team="foes", sheet=_sheet(might=2, nerve=1)),
    ]
    a_events, a_out = run_encounter(POOL, fighters, Rng(7), encounter_id="e7")
    b_events, b_out = run_encounter(POOL, fighters, Rng(7), encounter_id="e7")
    assert a_out.winner_team is not None and a_out == b_out
    assert [e.payload for e in a_events] == [e.payload for e in b_events]
    final = [e.payload["sheet"] for e in a_events if e.event_type == "SheetUpdated"]
    assert final and all("wounds" in s and "hp" not in s for s in final)


def test_the_registry_resolves_uro_pool() -> None:
    assert isinstance(registry.resolve("uro-pool"), UroPool)
    assert "uro-pool" in registry.available()


class _Scripted:
    def __init__(self, *, plan_json: str) -> None:
        self._plan = plan_json

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "Ash lunges."

    async def complete(self, req: CompletionRequest) -> str:
        return self._plan if req.stage_tag == "planner" else '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


async def test_a_uro_pool_fight_through_the_pipeline(store: PostgresEventStore) -> None:
    world = await store.create_world(f"Pool-{new_id()}", ruleset_id="uro-pool", ruleset_version="0")
    main = world.main_branch_id
    campaign = await store.start_campaign(
        world.world_id,
        main,
        participant_id="p1",
        new_pc_name="Ash",
        new_pc_id="a:ash",
        pc_sheet=_sheet(might=5, nerve=4),  # strong, so the fight resolves quickly
        ruleset_id="uro-pool",
        ruleset_version="0",
    )
    await store.append_beat(
        main,
        [
            actor_created(actor_id="a:bane", name="Bane", tier=2, role="thug"),
            sheet_updated(actor_id="a:bane", sheet=_sheet(might=1, nerve=1), ruleset_id="uro-pool"),
        ],
    )
    ruleset = registry.resolve(campaign.ruleset_id, campaign.ruleset_version)
    assert isinstance(ruleset, UroPool)
    plan = (
        '{"intent_class":"action","triggers":["violence"],'
        '"mechanics":[{"affordance":"strike","actor":"a:ash","target":"a:bane"}]}'
    )
    engine = Engine(
        store, ProviderRouter(bindings={}, default=_Scripted(plan_json=plan)), ruleset=ruleset
    )
    result = await engine.run_beat(campaign, "p1", "I go for Bane")

    async with store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT event_type, payload FROM events WHERE commit_id = $1 ORDER BY seq",
            result.commit_id,
        )
    types = [r["event_type"] for r in rows]
    assert "EncounterStarted" in types and "EncounterEnded" in types
    results = {r["payload"]["result"] for r in rows if r["event_type"] == "EncounterTurnTaken"}
    assert results and results <= {"act", "wound", "parried"}  # Uro Pool's own vocabulary
    bane = Sheet.model_validate(await store.get_sheet(main, "a:bane"))
    ash = Sheet.model_validate(await store.get_sheet(main, "a:ash"))
    assert not bane.in_fight or not ash.in_fight  # a decisive, ruleset-shaped result persisted


# --- holistic-review fixes (2026-10-30) ----------------------------------------------------


def test_a_round_close_keeps_every_strike_in_the_turn_trace() -> None:
    # Review: the runner kept only the FIRST effect's trace, so in a simultaneous round the other
    # fighter's strike never reached the narrator.
    fighters = [
        Combatant(actor_id="a:ash", team="party", sheet=_sheet(might=2, nerve=2)),
        Combatant(actor_id="a:bane", team="foes", sheet=_sheet(might=2, nerve=2)),
    ]
    events, _ = run_encounter(POOL, fighters, Rng(3), encounter_id="e3")
    closing = next(
        e for e in events if e.event_type == "EncounterTurnTaken" and e.payload["trace"]
    )  # the first round's closing commit
    assert "a:ash strikes a:bane" in closing.payload["trace"]
    assert "a:bane strikes a:ash" in closing.payload["trace"]


async def test_a_mutual_knockout_wounds_both_sides_and_loots_nothing(
    store: PostgresEventStore,
) -> None:
    from uro_core.domain.events import item_created
    from uro_core.rulesets.base import EncounterOutcome

    world = await store.create_world(f"Pool-{new_id()}")
    branch = world.main_branch_id
    await store.append_beat(
        branch,
        [
            actor_created(actor_id="a:ash", name="Ash"),
            actor_created(actor_id="a:bane", name="Bane"),
            item_created(item_id="i:knife", name="knife", owner_ref="a:bane"),
        ],
    )
    fighters = [
        Combatant(actor_id="a:ash", team="party", sheet={}),
        Combatant(actor_id="a:bane", team="foes", sheet={}),
    ]
    engine = Engine(store, ProviderRouter(bindings={}, default=_Scripted(plan_json="{}")))
    events = await engine._combat_consequences(
        branch, EncounterOutcome(winner_team=None, out_of_fight=["a:ash", "a:bane"]), fighters
    )
    wounded = {e.payload["subject_refs"][0] for e in events if e.event_type == "ClaimRecorded"}
    assert wounded == {"a:ash", "a:bane"}  # both went down — both carry the injury
    assert [e for e in events if e.event_type == "ItemTransferred"] == []  # nobody left to loot


def test_check_difficulties_reach_the_dice() -> None:
    # Review: no affordance set a difficulty, so every in-play check rolled against 4+.
    from uro_core.pipeline.mechanics import resolve_mechanics
    from uro_core.pipeline.plan import BeatPlan, PlanMechanic

    targets = {a.id: a.difficulty for a in POOL.affordances() if not a.starts_encounter}
    assert targets == {"investigate": "hard", "talk_down": "normal", "hold_firm": "easy"}
    plan = BeatPlan(mechanics=[PlanMechanic(affordance="investigate")])
    [result] = resolve_mechanics(POOL, plan, {"a:pc": _sheet(wits=3)}, "a:pc", Rng(0))
    assert result.detail["target"] == 5  # "hard" → 5+
