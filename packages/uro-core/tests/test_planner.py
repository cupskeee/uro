"""Phase 3 inc 3.2: the planned, mechanics-aware free-roam pipeline (docs/05, 13, D-28).

Pure tests cover plan validation (the affordance fence + D-21 trigger coverage) and the
mechanics gate; DB tests drive a full free-roam beat where the planner picks an affordance,
the ruleset resolves the check deterministically, and the outcome reaches the narrator.
"""

from collections.abc import AsyncIterator

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import actor_created, beat_resolved
from uro_core.domain.ids import new_id
from uro_core.errors import PlannerError
from uro_core.pipeline.engine import Engine
from uro_core.pipeline.mechanics import resolve_mechanics
from uro_core.pipeline.plan import BeatPlan, PlanMechanic, parse_plan, validate_plan
from uro_core.providers.adapters.stub import StubProvider, hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets.base import CharSpec
from uro_core.rulesets.rng import Rng
from uro_core.rulesets.uro_basic import UroBasic

RS = UroBasic()
_AFF = RS.affordances()


def _pc_sheet(abilities: dict[str, int] | None = None) -> dict:
    return RS.new_character(CharSpec(abilities=abilities), Rng(0)).model_dump()


# --- plan validation (deterministic; docs/13, D-21) ---


def test_validate_plan_fences_to_the_ruleset_vocabulary() -> None:
    plan = BeatPlan(mechanics=[PlanMechanic(affordance="teleport")])
    errors = validate_plan(plan, _AFF, {"a:pc"})
    assert any("unknown affordance" in e for e in errors)


def test_validate_plan_requires_known_actor_refs() -> None:
    plan = BeatPlan(mechanics=[PlanMechanic(affordance="persuade", actor="a:pc", target="a:ghost")])
    errors = validate_plan(plan, _AFF, {"a:pc"})  # a:ghost not known
    assert any("unknown actor 'a:ghost'" in e for e in errors)
    # with the target known, it validates
    assert validate_plan(plan, _AFF, {"a:pc", "a:ghost"}) == []


def test_validate_plan_enforces_trigger_coverage() -> None:
    # A recognized trigger with no affordance invoking it is rejected (D-21: no phrasing-dodge).
    bad = BeatPlan(triggers=["change_disposition"], mechanics=[])
    assert any("D-21" in e for e in validate_plan(bad, _AFF, set()))
    # Covered by a persuade mechanic → valid.
    good = BeatPlan(
        triggers=["change_disposition"],
        mechanics=[PlanMechanic(affordance="persuade", actor="a:pc")],
    )
    assert validate_plan(good, _AFF, {"a:pc"}) == []


def test_validate_plan_ignores_invented_trigger_categories() -> None:
    # A small model routinely invents NON-mechanical trigger categories (found live: gpt-4o-mini
    # emitting "social"/"movement"). D-21 governs only the ruleset's DECLARED vocabulary — an
    # invented category has no affordance, hence no check to dodge, so it must not fail the beat.
    invented = BeatPlan(triggers=["social", "movement"], mechanics=[])
    assert validate_plan(invented, _AFF, set()) == []
    # ...but a real, declared trigger with no mechanic still fails (D-21 preserved for real checks).
    real = BeatPlan(triggers=["violence"], mechanics=[])
    assert any("D-21" in e for e in validate_plan(real, _AFF, set()))


# --- mechanics gate (deterministic; docs/06) ---


def test_resolve_mechanics_resolves_a_check_deterministically() -> None:
    plan = BeatPlan(mechanics=[PlanMechanic(affordance="persuade", actor="a:pc", target="a:npc")])
    sheets = {"a:pc": _pc_sheet({"CHA": 16})}
    r1 = resolve_mechanics(RS, plan, sheets, "a:pc", Rng(5))
    r2 = resolve_mechanics(RS, plan, sheets, "a:pc", Rng(5))
    assert len(r1) == 1 and r1[0].ability == "CHA" and r1[0].modifier == 3  # CHA 16 → +3
    assert r1[0].model_dump() == r2[0].model_dump()  # same seed → same result
    assert r1[0].dc == RS.dc_for("medium")  # persuade is a medium-difficulty affordance


def test_resolve_mechanics_skips_encounter_and_unsheeted() -> None:
    # attack starts an encounter → not a single check (inc 3.3); a mechanic on an unsheeted
    # actor is skipped (nothing to roll).
    plan = BeatPlan(
        mechanics=[
            PlanMechanic(affordance="attack", actor="a:pc", target="a:npc"),
            PlanMechanic(affordance="sneak", actor="a:nobody"),
        ]
    )
    assert resolve_mechanics(RS, plan, {"a:pc": _pc_sheet()}, "a:pc", Rng(1)) == []


# --- sheets as state ---


async def test_start_campaign_assigns_sheet_and_fork_carries_it(store: PostgresEventStore) -> None:
    world = await store.create_world(f"test-{new_id()}")
    main = world.main_branch_id
    sheet = _pc_sheet({"STR": 15})
    await store.start_campaign(
        world.world_id,
        main,
        participant_id="p1",
        new_pc_name="Hero",
        new_pc_id="a:hero",
        pc_sheet=sheet,
        ruleset_id="uro-basic",
    )
    got = await store.get_sheet(main, "a:hero")
    assert got is not None and got["abilities"]["STR"] == 15 and got["hp"] == sheet["hp"]

    # the sheet is a projection — a fork carries it (materialization reproduces it).
    head = await store.get_branch(main)
    fork = await store.fork_branch(world.world_id, head.head_commit, "sibling")
    assert (await store.get_sheet(fork.branch_id, "a:hero"))["abilities"]["STR"] == 15


# --- full free-roam beat through the planner + gate ---


class _Scripted:
    """A provider serving canned planner/extractor completions; its narration echoes the
    system context, so a test can assert the mechanics trace reached the narrator."""

    def __init__(self, *, plan_json: str, narration: str = "The guard hesitates.") -> None:
        self._plan = plan_json
        self._narration = narration

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        ctx = " || ".join(m.content for m in req.messages if m.role == "system")
        yield f"{self._narration} [ctx: {ctx}]"

    async def complete(self, req: CompletionRequest) -> str:
        if req.stage_tag == "planner":
            return self._plan
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


async def _campaign_with_guard(store: PostgresEventStore):
    world = await store.create_world(f"test-{new_id()}")
    campaign = await store.start_campaign(
        world.world_id,
        world.main_branch_id,
        participant_id="player-1",
        new_pc_name="Hero",
        new_pc_id="a:hero",
        pc_sheet=_pc_sheet({"CHA": 16}),
        ruleset_id="uro-basic",
    )
    # a named NPC on stage so recall + the planner have a target
    await store.append_beat(
        campaign.branch_id, [actor_created(actor_id="a:guard", name="Guard", tier=1, role="sentry")]
    )
    return campaign


async def test_free_roam_beat_resolves_a_check_and_narrator_sees_it(
    store: PostgresEventStore,
) -> None:
    campaign = await _campaign_with_guard(store)
    plan = (
        '{"intent_class":"dialogue","triggers":["change_disposition"],'
        '"mechanics":[{"affordance":"persuade","actor":"a:hero","target":"a:guard"}],'
        '"narration_directives":"tense","suggestions":["bribe the guard","try intimidation"]}'
    )
    engine = Engine(
        store, ProviderRouter(bindings={}, default=_Scripted(plan_json=plan)), ruleset=RS
    )

    result = await engine.run_beat(campaign, "player-1", "I persuade the Guard to let me pass")
    assert result.checks == 1  # the CHA check was resolved
    assert "CHA check" in result.narration  # the roll trace reached the narrator
    assert result.suggestions == ["bribe the guard", "try intimidation"]  # D-23 hints surfaced


async def test_planner_reasks_then_succeeds(store: PostgresEventStore) -> None:
    campaign = await _campaign_with_guard(store)

    class _Reasker:
        def __init__(self) -> None:
            self._planner_calls = 0

        async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
            yield "ok"

        async def complete(self, req: CompletionRequest) -> str:
            if req.stage_tag != "planner":
                return '{"actors": [], "claims": []}'
            self._planner_calls += 1
            if self._planner_calls == 1:
                return "not json at all"  # first plan invalid → re-ask
            return '{"intent_class":"action","triggers":[],"mechanics":[]}'  # then valid

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [hashing_embedding(t) for t in texts]

    reasker = _Reasker()
    engine = Engine(store, ProviderRouter(bindings={}, default=reasker), ruleset=RS)
    result = await engine.run_beat(campaign, "player-1", "I look around")
    assert reasker._planner_calls == 2 and result.checks == 0  # recovered on the re-ask


async def test_planner_exhausts_reasks_and_beat_fails(store: PostgresEventStore) -> None:
    campaign = await _campaign_with_guard(store)
    # Always an unknown affordance → never validates → PlannerError after re-asks, nothing commits.
    bad_plan = '{"mechanics":[{"affordance":"teleport"}]}'
    engine = Engine(
        store, ProviderRouter(bindings={}, default=_Scripted(plan_json=bad_plan)), ruleset=RS
    )
    before = len(await store.recent_beats(campaign.branch_id, 50))
    with pytest.raises(PlannerError):
        await engine.run_beat(campaign, "player-1", "I cast teleport")
    assert len(await store.recent_beats(campaign.branch_id, 50)) == before  # no partial commit


async def test_no_ruleset_skips_planner(store: PostgresEventStore) -> None:
    # Backward compat: with no ruleset bound the pipeline is the Phase-1 flow — the planner
    # never runs (a provider that would REJECT a planner call still produces a clean beat).
    class _NoPlanner:
        async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
            yield "the fire crackles"

        async def complete(self, req: CompletionRequest) -> str:
            assert req.stage_tag != "planner", "planner must not be called without a ruleset"
            return '{"actors": [], "claims": []}'

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [hashing_embedding(t) for t in texts]

    world = await store.create_world(f"test-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    engine = Engine(store, ProviderRouter(bindings={}, default=_NoPlanner()))  # no ruleset
    result = await engine.run_beat(campaign, "player-1", "look around")
    assert result.checks == 0 and result.suggestions == []


async def test_beat_rng_is_reproducible_and_distinct_per_beat(store: PostgresEventStore) -> None:
    # Replay determinism substrate: same history → same seed; a new beat (new head) → new seed.
    campaign = await _campaign_with_guard(store)
    engine = Engine(store, ProviderRouter(bindings={}, default=StubProvider()), ruleset=RS)
    other = Engine(store, ProviderRouter(bindings={}, default=StubProvider()), ruleset=RS)
    rng_a = await engine._beat_rng(campaign)
    rng_b = await other._beat_rng(campaign)
    assert rng_a.seed == rng_b.seed  # reproducible for the same campaign + head
    await store.append_beat(
        campaign.branch_id,
        [beat_resolved(beat_id="b", participant_id="p", intent_text="x", narration="y")],
    )
    assert (await engine._beat_rng(campaign)).seed != rng_a.seed  # head advanced → distinct


async def test_adopted_pc_gets_sheeted_and_is_checked(store: PostgresEventStore) -> None:
    # Review fix (the medium): an adopted PC must be sheeted so the mechanics gate is not inert.
    world = await store.create_world(f"test-{new_id()}")
    main = world.main_branch_id
    await store.append_beat(main, [actor_created(actor_id="a:wizard", name="Wizard", tier=2)])
    assert await store.get_sheet(main, "a:wizard") is None  # an NPC with no sheet
    # adopt it WITH a sheet — exactly what the fixed CLI does for an unsheeted adopted actor
    campaign = await store.start_campaign(
        world.world_id,
        main,
        participant_id="p1",
        adopt_actor_id="a:wizard",
        pc_sheet=_pc_sheet({"CHA": 16}),
        ruleset_id="uro-basic",
    )
    assert await store.get_sheet(main, "a:wizard") is not None  # now sheeted
    await store.append_beat(main, [actor_created(actor_id="a:guard", name="Guard", tier=1)])
    plan = (
        '{"intent_class":"dialogue","triggers":["change_disposition"],'
        '"mechanics":[{"affordance":"persuade","actor":"a:wizard","target":"a:guard"}]}'
    )
    engine = Engine(
        store, ProviderRouter(bindings={}, default=_Scripted(plan_json=plan)), ruleset=RS
    )
    result = await engine.run_beat(campaign, "p1", "I persuade the Guard")
    assert result.checks == 1  # the adopted PC's sheet WAS used — gate not inert


# --- parse_plan robustness: salvage a small model's near-misses (found live, gpt-4o-mini) ---


def test_parse_plan_salvages_small_model_near_misses() -> None:
    import json

    raw = json.dumps(
        {
            "intent_class": "conversation",  # NOT in the Literal → coerce to "action"
            "triggers": "violence",  # a string, not a list → wrap
            "mechanics": [
                {"affordance": "attack", "target": "a:foe"},
                {"note": "no affordance key"},  # junk entry → drop
            ],
            "mode_transition": "encounter",  # a string, not {"to": …} → drop
            "narration_directives": "keep it tense",
        }
    )
    plan = parse_plan(raw)
    assert plan is not None  # the beat is NOT lost to a near-miss
    assert plan.intent_class == "action"
    assert plan.triggers == ["violence"]
    assert plan.mode_transition is None
    assert [m.affordance for m in plan.mechanics] == ["attack"]  # valid kept, junk dropped
    assert plan.narration_directives == "keep it tense"


def test_parse_plan_keeps_a_valid_plan_and_handles_fences() -> None:
    good = parse_plan('{"intent_class": "dialogue", "triggers": [], "mechanics": []}')
    assert good is not None and good.intent_class == "dialogue"
    fenced = parse_plan('```json\n{"intent_class": "examine", "mechanics": []}\n```')
    assert fenced is not None and fenced.intent_class == "examine"  # markdown fence tolerated
    assert parse_plan("not json at all") is None  # truly unusable → None (drives a re-ask)
