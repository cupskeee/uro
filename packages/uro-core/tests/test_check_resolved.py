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
    # `per_event: true` makes the rule fire for EACH failed check — without it a rule fires once per
    # beat, so two failures in one beat would raise the heat by one (review).
    heat_up = {
        "id": "failure-raises-heat",
        "trigger": {"event": "CheckResolved", "where": {"outcome": "failure"}, "per_event": True},
        "then": [{"do": "adjust_counter", "scope_ref": "f:vault", "key": "heat", "delta": 1}],
        "scope": {"world": True},
    }
    once = {**heat_up, "id": "once-per-beat", "trigger": {**heat_up["trigger"], "per_event": False}}
    once["then"] = [{"do": "adjust_counter", "scope_ref": "f:vault", "key": "beats", "delta": 1}]
    campaign = await _campaign(
        store, "uro-basic", {"rules_api_version": 7, "rules": [heat_up, once], "agendas": []}
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=_Stub()), ruleset=_AlwaysFails())
    plan = BeatPlan(mechanics=[PlanMechanic(affordance="sneak")] * 2)  # two failed checks a beat
    for _ in range(3):
        await engine.run_beat(campaign, "p1", "I slip past the guards", plan=plan)
    assert await store.get_counter(campaign.branch_id, "f:vault", "heat") == 6  # every failure
    assert await store.get_counter(campaign.branch_id, "f:vault", "beats") == 3  # once per beat

    class _AlwaysSucceeds(UroBasic):
        def resolve_check(self, req, rng):  # type: ignore[no-untyped-def]
            return CheckResult(outcome="success", trace="d20 (20) → success")

    lucky = Engine(store, ProviderRouter(bindings={}, default=_Stub()), ruleset=_AlwaysSucceeds())
    await lucky.run_beat(campaign, "p1", "I slip past the guards", plan=plan)
    assert await store.get_counter(campaign.branch_id, "f:vault", "heat") == 6  # a success: no heat


async def test_ashfall_talks_about_a_failed_check(store: PostgresEventStore) -> None:
    # The example pack's rule for the live pass (docs/live-run.md section 9): a failed check commits
    # a rumor about the Brinehouse, and a later scene there recalls it for the narrator.
    from pathlib import Path

    from uro_core.pipeline.recall import assemble_recall
    from uro_core.worldpack.create import import_pack
    from uro_core.worldpack.parse import parse_pack

    pack = parse_pack(Path(__file__).resolve().parents[3] / "worlds" / "ashfall")
    pack.manifest.name = f"Ashfall-{new_id()}"
    world = (await import_pack(store, pack)).world
    sheet = registry.resolve("uro-basic").new_character(CharSpec(), Rng(0))
    campaign = await store.start_campaign(
        world.world_id,
        world.main_branch_id,
        participant_id="p1",
        new_pc_name="Ash",
        new_pc_id=f"a:ash-{new_id()}",
        pc_sheet=sheet,
        ruleset_id="uro-basic",
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=_Stub()), ruleset=_AlwaysFails())
    plan = BeatPlan(mechanics=[PlanMechanic(affordance="sneak")])
    await engine.run_beat(campaign, "p1", "I slip into the warehouse", plan=plan)

    rumors = await store.claims_about(campaign.branch_id, "p:brinehouse")
    assert [(c.origin, c.truth) for c in rumors] == [("module", "unknown")]  # testimony, not canon
    recall = await assemble_recall(
        store, campaign.branch_id, "I go back to the Brinehouse and listen", 8
    )
    assert rumors[0].claim_id in {c.claim_id for c in recall.claims}  # the narrator is shown it
