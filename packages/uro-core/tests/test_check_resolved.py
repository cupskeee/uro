"""CheckResolved (D-64): every resolved free-roam check commits its outcome — in the ruleset's own
band — so a world's reaction rules can answer it. Seventh Vault (G-2) wanted exactly this: "every
failed check +1 heat; heat >= 3 → lockdown", which no event could express before.
"""

from collections.abc import AsyncIterator

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.ids import new_id
from uro_core.pipeline.engine import Engine
from uro_core.pipeline.plan import BeatPlan, PlanMechanic
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.rulesets import registry
from uro_core.rulesets.base import CharSpec, CheckResult
from uro_core.rulesets.rng import Rng
from uro_core.rulesets.uro_basic import UroBasic


class _Stub:
    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "The vault hums."

    async def complete(self, req: CompletionRequest) -> str:
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


class _AlwaysFails(UroBasic):
    def resolve_check(self, req, rng):  # type: ignore[no-untyped-def]
        return CheckResult(outcome="failure", trace="d20 (1) → failure")


async def _campaign(store: PostgresEventStore, ruleset_id: str, rule_pack: dict | None = None):  # type: ignore[no-untyped-def,type-arg]
    world = await store.create_world(
        f"Vault-{new_id()}", ruleset_id=ruleset_id, rule_pack=rule_pack
    )
    sheet = registry.resolve(ruleset_id).new_character(CharSpec(), Rng(0))
    return await store.start_campaign(
        world.world_id,
        world.main_branch_id,
        participant_id="p1",
        new_pc_name="Ash",
        new_pc_id=f"a:ash-{new_id()}",
        pc_sheet=sheet,
        ruleset_id=ruleset_id,
    )


async def test_each_check_commits_its_outcome(store: PostgresEventStore) -> None:
    campaign = await _campaign(store, "uro-pool")
    pc = await store.campaign_pc(campaign.campaign_id)
    engine = Engine(
        store, ProviderRouter(bindings={}, default=_Stub()), ruleset=registry.resolve("uro-pool")
    )
    plan = BeatPlan(
        mechanics=[PlanMechanic(affordance="talk_down"), PlanMechanic(affordance="investigate")]
    )
    preview = await engine.preview_beat(campaign, "p1", "I talk, then look", plan=plan)
    assert [e.event_type for e in preview].count("CheckResolved") == 2

    result = await engine.run_beat(campaign, "p1", "I talk, then look", plan=plan)
    async with store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT payload, caused_by FROM events WHERE commit_id = $1 "
            "AND event_type = 'CheckResolved' ORDER BY seq",
            result.commit_id,
        )
    assert [r["payload"]["affordance"] for r in rows] == ["talk_down", "investigate"]
    assert all(r["payload"]["actor_id"] == pc for r in rows)
    assert {r["payload"]["outcome"] for r in rows} <= {"fail", "success", "triumph"}  # Pool's band
    assert all(r["caused_by"]["kind"] == "ruleset" for r in rows)
    assert [r["payload"]["trace"] for r in rows] == result.check_traces


async def test_a_heat_meter_rises_on_every_failed_check(store: PostgresEventStore) -> None:
    heat_up = {
        "id": "failure-raises-heat",
        "trigger": {"event": "CheckResolved", "where": {"outcome": "failure"}},
        "then": [{"do": "adjust_counter", "scope_ref": "f:vault", "key": "heat", "delta": 1}],
        "scope": {"world": True},
    }
    campaign = await _campaign(
        store, "uro-basic", {"rules_api_version": 7, "rules": [heat_up], "agendas": []}
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=_Stub()), ruleset=_AlwaysFails())
    plan = BeatPlan(mechanics=[PlanMechanic(affordance="sneak")])
    for _ in range(3):
        await engine.run_beat(campaign, "p1", "I slip past the guard", plan=plan)
    assert await store.get_counter(campaign.branch_id, "f:vault", "heat") == 3

    succeeding = Engine(
        store, ProviderRouter(bindings={}, default=_Stub()), ruleset=registry.resolve("uro-basic")
    )
    for _ in range(4):  # some of these succeed: only the failures raise the heat
        await succeeding.run_beat(campaign, "p1", "I slip past the guard", plan=plan)
    async with store.pool.acquire() as conn:
        failures = await conn.fetchval(
            "SELECT count(*) FROM events e JOIN commits c ON c.commit_id = e.commit_id "
            "JOIN branches b ON b.world_id = c.world_id WHERE b.branch_id = $1 "
            "AND e.event_type = 'CheckResolved' AND e.payload->>'outcome' = 'failure'",
            campaign.branch_id,
        )
    assert await store.get_counter(campaign.branch_id, "f:vault", "heat") == failures
