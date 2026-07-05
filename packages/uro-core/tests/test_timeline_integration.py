"""Phase 0 acceptance in miniature: beats persist and a fresh store resumes them.

This is the core of the roadmap Phase 0 acceptance test — proving state lives in
Postgres, not process memory — with a deterministic stub provider standing in for
the LLM (coherence over 20 beats is a live-smoke concern, not a CI one).
"""

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.ids import new_id
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import StubProvider
from uro_core.providers.router import ProviderRouter


def _engine(store: PostgresEventStore) -> Engine:
    return Engine(store, ProviderRouter(bindings={}, default=StubProvider()))


async def test_beats_persist_and_a_fresh_store_resumes(store: PostgresEventStore) -> None:
    world = await store.create_world(f"test-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    engine = _engine(store)

    intents = ["look around the tavern", "order an ale", "ask the barkeep about rumors"]
    commit_ids = []
    for intent in intents:
        result = await engine.run_beat(campaign, "player-1", intent)
        assert result.narration  # stub produced prose
        assert intent in result.narration or "stub" in result.narration
        commit_ids.append(result.commit_id)
    assert len(set(commit_ids)) == 3  # each beat is its own commit

    # Resume with a BRAND NEW store object: state must come from Postgres, not memory.
    resumed = PostgresEventStore(store.dsn)
    await resumed.connect()
    try:
        camp2 = await resumed.get_campaign(campaign.campaign_id)
        assert camp2 is not None
        beats = await resumed.recent_beats(camp2.branch_id, 10)
        assert [b.intent_text for b in beats] == intents  # in order, from the log

        # And the resumed session can keep playing on top of that history.
        engine2 = _engine(resumed)
        await engine2.run_beat(camp2, "player-1", "leave through the back door")
        beats2 = await resumed.recent_beats(camp2.branch_id, 10)
        assert len(beats2) == 4
        assert beats2[-1].intent_text == "leave through the back door"
    finally:
        await resumed.close()


async def test_recency_window_caps_returned_beats(store: PostgresEventStore) -> None:
    world = await store.create_world(f"test-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    engine = _engine(store)
    for i in range(6):
        await engine.run_beat(campaign, "player-1", f"beat {i}")

    recent = await store.recent_beats(campaign.branch_id, 4)
    assert [b.intent_text for b in recent] == ["beat 2", "beat 3", "beat 4", "beat 5"]
