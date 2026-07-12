"""G-3 (docs/18): the per-beat mechanics RNG is reproducible run-to-run.

Before the fix Engine._beat_rng hashed (campaign_id : head_commit) — both freshly random per run
(new_id()) — so two runs of the same played campaign rolled different dice and a guaranteed-loss
fight occasionally flipped (a flaky gate). The fix reseeds from the campaign's persisted SEED + the
commit DEPTH (both deterministic), so a beat's rolls are a pure function of (seed, beat position).
"""

from collections.abc import AsyncIterator

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.ids import new_id
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter
from uro_core.timeline.models import Campaign


class _Stub:
    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield "x"

    async def complete(self, req: CompletionRequest) -> str:
        return "{}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


def _engine(store: PostgresEventStore) -> Engine:
    return Engine(store, ProviderRouter(bindings={}, default=_Stub()))


async def test_beat_rng_is_pure_function_of_seed_not_campaign_id(store: PostgresEventStore) -> None:
    world = await store.create_world(f"rng-{new_id()}")
    engine = _engine(store)
    main = world.main_branch_id
    # two DIFFERENT campaigns (distinct random campaign_ids) on the same branch, SAME seed:
    a = Campaign(campaign_id=new_id(), world_id=world.world_id, branch_id=main, seed=42)
    b = Campaign(campaign_id=new_id(), world_id=world.world_id, branch_id=main, seed=42)
    # same seed + same branch depth → identical RNG (independent of the random campaign_id) — the
    # crux of G-3; the pre-fix code, keyed on campaign_id, would have diverged here.
    assert (await engine._beat_rng(a)).seed == (await engine._beat_rng(b)).seed
    # a different seed → a different RNG
    c = Campaign(campaign_id=new_id(), world_id=world.world_id, branch_id=main, seed=43)
    assert (await engine._beat_rng(a)).seed != (await engine._beat_rng(c)).seed


async def test_beat_rng_advances_with_commit_depth(store: PostgresEventStore) -> None:
    from uro_core.domain.events import place_created

    world = await store.create_world(f"rng2-{new_id()}")
    engine = _engine(store)
    camp = Campaign(
        campaign_id=new_id(), world_id=world.world_id, branch_id=world.main_branch_id, seed=7
    )
    before = (await engine._beat_rng(camp)).seed
    # commit a beat: the branch head advances one generation → the next beat's RNG differs (so
    # successive beats in a campaign don't all share one roll stream position).
    await store.append_beat(world.main_branch_id, [place_created(place_id="p:x", name="X")])
    after = (await engine._beat_rng(camp)).seed
    assert before != after


async def test_campaign_seed_persists(store: PostgresEventStore) -> None:
    world = await store.create_world(f"rng3-{new_id()}")
    c = await store.start_campaign(
        world.world_id, world.main_branch_id, participant_id="p1", new_pc_name="Ash", seed=1234
    )
    assert c.seed == 1234
    reloaded = await store.get_campaign(c.campaign_id)
    assert reloaded is not None and reloaded.seed == 1234


async def test_out_of_int64_seed_is_rejected_cleanly(store: PostgresEventStore) -> None:
    # the seed lands in a BIGINT column — an out-of-range value must raise a clean ValueError
    # (→ CLI error / REST 400), not a raw asyncpg OverflowError at the INSERT.
    import pytest

    world = await store.create_world(f"rng4-{new_id()}")
    with pytest.raises(ValueError, match="out of range"):
        await store.start_campaign(
            world.world_id, world.main_branch_id, participant_id="p1", new_pc_name="A", seed=2**63
        )
