"""D-98: a streamed beat hands its caller what the engine knows about it — the ruleset's check
results, suggestions, the beat's mode, the in-fiction day after it, and the entities it made —
which `beat_committed`, `uro play` and `uro connect` show beside the narration."""

import json

from uro_cli.main import beat_detail_lines
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.ids import new_id
from uro_core.pipeline.engine import BeatResult, Engine
from uro_core.providers.adapters.stub import StubProvider
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter


class _Naming(StubProvider):
    """The stub's narration; an extractor that names one new person and one new place."""

    async def complete(self, req: CompletionRequest) -> str:
        if req.stage_tag != "extractor":
            return await super().complete(req)
        return json.dumps(
            {
                "actors": [{"name": "Jun Abe", "role": "pilot"}],
                "places": [{"name": "The Dive Lock", "description": "Where divers lock out."}],
                "claims": [],
            }
        )


async def test_the_stream_hands_back_the_beat(store: PostgresEventStore) -> None:
    world = await store.create_world(f"details-{new_id()}")
    campaign = await store.start_campaign(
        world.world_id, world.main_branch_id, participant_id="p1", new_pc_name="Sam"
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=_Naming()))
    got: list[BeatResult] = []
    async for _ in engine.run_beat_stream(
        campaign, "p1", "I sleep until morning.", on_result=got.append
    ):
        pass
    (result,) = got
    assert result.mode == "freeroam" and result.world_day == 1  # the day the intent passed
    assert {(c.kind, c.name) for c in result.created} >= {
        ("actor", "Jun Abe"),
        ("place", "The Dive Lock"),
    }
    head = (await store.get_branch(campaign.branch_id)).head_commit  # type: ignore[union-attr]
    assert result.commit_id != "" and head is not None


def test_the_cli_shows_the_details() -> None:
    lines = beat_detail_lines(
        {
            "checks": [{"actor_id": "a:sam", "affordance": "persuade", "outcome": "success"}],
            "mode": "encounter",
            "world_day": 4,
            "created": [{"kind": "actor", "id": "a:abe", "name": "Jun Abe"}],
            "suggestions": ["ask about the dive", "check the lock"],
        },
        previous_day=3,
    )
    assert lines == [
        "  · persuade: success",
        "  · a fight was resolved",
        "  · day 4",
        "  + new: Jun Abe (actor)",
        "  ? you could: ask about the dive; check the lock",
    ]
    assert beat_detail_lines({"world_day": 4}, previous_day=4) == []  # nothing new, no lines
    assert beat_detail_lines({"world_day": 4}, previous_day=None) == ["  · day 4"]  # first seen
    others = beat_detail_lines({"suggestions": ["hide"]}, previous_day=None, mine=False)
    assert others == []  # another player's suggestions aren't offered to you


async def test_the_servers_beat_runner_yields_the_result_last(store: PostgresEventStore) -> None:
    # the real engine_deps wiring (not a fake run_beat): narration, then the BeatResult
    from uro_server.app import engine_deps

    world = await store.create_world(f"runner-{new_id()}")
    campaign = await store.start_campaign(
        world.world_id, world.main_branch_id, participant_id="p1", new_pc_name="Sam"
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=_Naming()))
    deps = engine_deps(store, engine, {"tok": "p1"})
    items = [x async for x in deps.run_beat(campaign.campaign_id, "p1", "I look around.")]
    assert isinstance(items[-1], BeatResult)
    assert all(isinstance(x, str) for x in items[:-1])
    assert {c.name for c in items[-1].created} >= {"Jun Abe", "The Dive Lock"}
