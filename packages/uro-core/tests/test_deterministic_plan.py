"""B9 (#8): a client-supplied plan — the DETERMINISTIC path into the mechanics gate (no LLM).

`Engine.run_beat(..., plan=BeatPlan)` drives the planner→mechanics gate WITHOUT the LLM, so CI and
keyless consumers can resolve checks deterministically. The supplied plan is validated exactly like
an LLM plan (affordance fence + D-21 trigger coverage).
"""

from collections.abc import AsyncIterator

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import actor_created, sheet_updated
from uro_core.domain.ids import new_id
from uro_core.errors import PlannerError
from uro_core.pipeline.engine import Engine
from uro_core.pipeline.plan import BeatPlan, PlanMechanic
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets.base import CharSpec
from uro_core.rulesets.rng import Rng
from uro_core.rulesets.uro_basic import UroBasic

RS = UroBasic()

# a valid free-roam check for Uro Basic: persuade (trigger category change_disposition)
_PLAN = BeatPlan(triggers=["change_disposition"], mechanics=[PlanMechanic(affordance="persuade")])


class _StubNarrator:
    """Streams a little narration; the extractor gets nothing. NO scripted planner — that's the
    point: the mechanics resolve from the SUPPLIED plan, not from a canned provider response."""

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "The guard hesitates."

    async def complete(self, req: CompletionRequest) -> str:
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


def _engine(store: PostgresEventStore) -> Engine:
    return Engine(store, ProviderRouter(bindings={}, default=_StubNarrator()), ruleset=RS)


async def _campaign(store: PostgresEventStore, seed: int = 7):
    world = await store.create_world(f"b9-{new_id()}")
    return await store.start_campaign(
        world.world_id,
        world.main_branch_id,
        participant_id="p1",
        new_pc_name="Ada",
        new_pc_id="a:ada",
        pc_sheet=RS.new_character(CharSpec(data={"abilities": {"CHA": 14}}), Rng(0)),
        ruleset_id="uro-basic",
        seed=seed,
    )


async def test_client_plan_resolves_a_check_without_an_llm_planner(
    store: PostgresEventStore,
) -> None:
    campaign = await _campaign(store)
    r = await _engine(store).run_beat(campaign, "p1", "I sweet-talk the guard", plan=_PLAN)
    assert r.checks == 1  # the persuade check resolved
    assert len(r.check_traces) == 1 and r.check_traces[0]  # …with a trace, not just a count


async def test_no_plan_with_the_stub_resolves_no_checks(store: PostgresEventStore) -> None:
    # the gap B9 closes: without a supplied plan, the stub planner is a no-op → 0 checks resolve.
    campaign = await _campaign(store)
    r = await _engine(store).run_beat(campaign, "p1", "I sweet-talk the guard")
    assert r.checks == 0


async def test_client_plan_is_deterministic(store: PostgresEventStore) -> None:
    # same seed → byte-identical check outcome (the pinned mechanics RNG, G-3).
    c1 = await _campaign(store, seed=42)
    c2 = await _campaign(store, seed=42)
    t1 = (
        await _engine(store).run_beat(c1, "p1", "I sweet-talk the guard", plan=_PLAN)
    ).check_traces
    t2 = (
        await _engine(store).run_beat(c2, "p1", "I sweet-talk the guard", plan=_PLAN)
    ).check_traces
    assert t1 == t2


async def test_invalid_supplied_plan_is_rejected(store: PostgresEventStore) -> None:
    # a supplied plan is fenced exactly like an LLM plan: an unknown affordance is refused.
    campaign = await _campaign(store)
    bad = BeatPlan(mechanics=[PlanMechanic(affordance="teleport")])
    with pytest.raises(PlannerError):
        await _engine(store).run_beat(campaign, "p1", "I teleport away", plan=bad)


async def test_client_plan_can_start_an_encounter_and_traces_surface(
    store: PostgresEventStore,
) -> None:
    # The highest-consequence B9 path: a supplied plan whose affordance STARTS a fight drives the
    # encounter deterministically (no LLM). The fight's rounds must surface in check_traces — a
    # free-roam-only gate reported an EMPTY combat beat (phase-end review, critic-2).
    campaign = await _campaign(store)
    await store.append_beat(
        campaign.branch_id,
        [
            actor_created(actor_id="a:grull", name="Grull", tier=2, role="brute"),
            sheet_updated(
                actor_id="a:grull",
                sheet=RS.new_character(
                    CharSpec(data={"abilities": {"STR": 20, "DEX": 14, "CON": 20}}), Rng(0)
                ),
                ruleset_id="uro-basic",
            ),
        ],
    )
    attack = BeatPlan(
        triggers=["violence"],
        mechanics=[PlanMechanic(affordance="attack", actor="a:ada", target="a:grull")],
    )
    r = await _engine(store).run_beat(campaign, "p1", "I swing at Grull", plan=attack)
    assert r.check_traces  # the fight's rounds surfaced — not the empty count a combat beat gave
    assert r.checks == len(r.check_traces)  # count and traces stay in lockstep
