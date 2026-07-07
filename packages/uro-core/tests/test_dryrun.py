"""Phase 4 inc 4.4: dry-run (docs/09 creator loop). A beat's pipeline runs and returns the
would-be events, but nothing is committed — the campaign state is untouched.
"""

from collections.abc import AsyncIterator

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter


class _Provider:
    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "A hooded figure named Sela watches from the corner."

    async def complete(self, req: CompletionRequest) -> str:
        return '{"actors": [{"name": "Sela", "role": "watcher"}], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


async def test_preview_beat_computes_events_without_committing(store: PostgresEventStore) -> None:
    world = await store.create_world("Dry")
    campaign = await store.start_campaign(
        world.world_id, world.main_branch_id, participant_id="p1", new_pc_name="A", new_pc_id="a:a"
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=_Provider()))
    head_before = (await store.get_branch(world.main_branch_id)).head_commit

    events = await engine.preview_beat(campaign, "p1", "I look around")

    types = [e.event_type for e in events]
    assert "BeatResolved" in types and "ActorCreated" in types  # the would-be diff
    # ...but NOTHING was committed: the head is unchanged and Sela never entered state
    assert (await store.get_branch(world.main_branch_id)).head_commit == head_before
    assert await store.find_actor_by_name(world.main_branch_id, "Sela") is None
