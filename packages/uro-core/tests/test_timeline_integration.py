"""Phase 0 acceptance in miniature: beats persist and a fresh store resumes them.

This is the core of the roadmap Phase 0 acceptance test — proving state lives in
Postgres, not process memory — with a deterministic stub provider standing in for
the LLM (coherence over 20 beats is a live-smoke concern, not a CI one).
"""

from collections.abc import AsyncIterator

import pytest
from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import beat_resolved
from uro_core.domain.ids import new_id
from uro_core.errors import EmptyNarrationError
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import StubProvider
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter


def _engine(store: PostgresEventStore) -> Engine:
    return Engine(store, ProviderRouter(bindings={}, default=StubProvider()))


class _SilentProvider:
    """Yields no content — models an empty/content-filtered completion."""

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        return
        yield ""  # pragma: no cover - marks this an async generator

    async def complete(self, req: CompletionRequest) -> str:
        return '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 8 for _ in texts]


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


async def test_recent_beats_orders_a_multi_event_commit(store: PostgresEventStore) -> None:
    # Guards the ORDER BY fix (review F1): a commit with >1 BeatResolved must return
    # oldest-first, and a tight limit must keep the NEWEST, still oldest-first.
    world = await store.create_world(f"test-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    events = [
        beat_resolved(
            beat_id=f"b{i}", participant_id="p1", intent_text=f"intent {i}", narration=f"n{i}"
        )
        for i in range(3)
    ]
    await store.append_beat(campaign.branch_id, events)

    all_three = await store.recent_beats(campaign.branch_id, 10)
    assert [b.intent_text for b in all_three] == ["intent 0", "intent 1", "intent 2"]

    newest_two = await store.recent_beats(campaign.branch_id, 2)
    assert [b.intent_text for b in newest_two] == ["intent 1", "intent 2"]


async def test_empty_narration_is_rejected_and_nothing_commits(store: PostgresEventStore) -> None:
    world = await store.create_world(f"test-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    engine = Engine(store, ProviderRouter(bindings={}, default=_SilentProvider()))

    with pytest.raises(EmptyNarrationError):
        await engine.run_beat(campaign, "player-1", "say nothing")

    assert await store.recent_beats(campaign.branch_id, 10) == []


async def test_llm_calls_are_metered(store: PostgresEventStore) -> None:
    world = await store.create_world(f"test-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    engine = _engine(store)
    await engine.run_beat(campaign, "player-1", "look around")

    async with store.pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM llm_calls WHERE stage_tag = 'narrator'")
    assert count >= 1
