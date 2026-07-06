"""Phase 1 increment 3: semantic memory (pgvector) and semantic recall.

The stub embedder is a deterministic bag-of-words vectorizer, so a query sharing
words with a memory ranks it first — letting the "old memory resurfaces" behavior
be tested offline, not just the plumbing.
"""

import math
from collections.abc import AsyncIterator

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.ids import new_id
from uro_core.errors import ProviderError
from uro_core.pipeline.engine import Engine
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter


class ScriptedProvider:
    """Fixed narration; queued extractor JSON; deterministic (hashing) embeddings."""

    def __init__(
        self, *, narration: str = "The fire crackles.", completions: list[str] | None = None
    ):
        self._narration = narration
        self._completions = list(completions or [])

    async def stream(self, req: CompletionRequest) -> AsyncIterator[str]:
        yield self._narration

    async def complete(self, req: CompletionRequest) -> str:
        return self._completions.pop(0) if self._completions else '{"actors": [], "claims": []}'

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [hashing_embedding(t) for t in texts]


class _WrongDimProvider(ScriptedProvider):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 8 for _ in texts]  # 8-dim vs stored 256-dim → search dim-mismatch


class _RaisingEmbedProvider(ScriptedProvider):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise ProviderError("embedder is down")


async def _branch(store: PostgresEventStore) -> str:
    world = await store.create_world(f"test-{new_id()}")
    return world.main_branch_id


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))  # both are L2-normalized


def test_hashing_embedding_is_deterministic_and_semantic() -> None:
    assert hashing_embedding("the Duke") == hashing_embedding("the Duke")  # deterministic
    dukes = _cosine(
        hashing_embedding("the Duke and his army"), hashing_embedding("the Duke's army")
    )
    cats = _cosine(
        hashing_embedding("the Duke and his army"), hashing_embedding("a cat by the fire")
    )
    assert dukes > cats  # shared words → higher similarity
    assert math.isclose(
        _cosine(hashing_embedding("x y"), hashing_embedding("x y")), 1.0, abs_tol=1e-6
    )


async def test_vector_search_finds_the_nearest_memory(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    duke = "The Duke disbanded his army years ago."
    cat = "A cat sleeps by the tavern fire."
    for cid, text in [("c1", duke), ("c2", cat)]:
        await store.add_memory(
            branch_id=branch,
            commit_id=cid,
            kind="beat",
            text=text,
            vector=hashing_embedding(text),
            entity_refs=[],
        )
    hits = await store.search(
        branch, hashing_embedding("what became of the Duke and his army"), k=1
    )
    assert len(hits) == 1 and hits[0].text == duke


async def test_vector_search_is_branch_scoped(store: PostgresEventStore) -> None:
    b1, b2 = await _branch(store), await _branch(store)
    text = "A secret about the vault under the chapel."
    await store.add_memory(
        branch_id=b1,
        commit_id="c",
        kind="beat",
        text=text,
        vector=hashing_embedding(text),
        entity_refs=[],
    )
    query = hashing_embedding("the vault secret")
    assert await store.search(b2, query, k=5) == []  # another branch can't see it
    assert len(await store.search(b1, query, k=5)) == 1


async def test_vectors_are_deduplicated_by_content(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    text = "The same remembered line."
    for cid in ("c1", "c2"):
        await store.add_memory(
            branch_id=branch,
            commit_id=cid,
            kind="beat",
            text=text,
            vector=hashing_embedding(text),
            entity_refs=[],
        )
    async with store.pool.acquire() as conn:
        # scope to this branch — the embeddings table is global and shared across tests.
        n_distinct_vectors = await conn.fetchval(
            "SELECT count(DISTINCT content_hash) FROM memory_index WHERE branch_id = $1", branch
        )
        n_memories = await conn.fetchval(
            "SELECT count(*) FROM memory_index WHERE branch_id = $1", branch
        )
    assert n_distinct_vectors == 1 and n_memories == 2  # one vector, two membership rows


async def test_engine_beat_becomes_a_searchable_memory(store: PostgresEventStore) -> None:
    world = await store.create_world(f"test-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    provider = ScriptedProvider(
        narration="The blacksmith forged a silver key for the north tower.",
        completions=['{"actors": [], "claims": []}'],
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=provider))
    await engine.run_beat(campaign, "player-1", "I visit the blacksmith")

    hits = await store.search(campaign.branch_id, hashing_embedding("silver key blacksmith"), k=5)
    assert any("silver key" in h.text for h in hits)


async def test_recall_resurfaces_an_old_memory_out_of_window(store: PostgresEventStore) -> None:
    # The Phase-1 acceptance half: a memory far outside the recency window resurfaces
    # when the current intent is thematically related.
    world = await store.create_world(f"test-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    branch = campaign.branch_id
    old = "The oracle warned of a great flood in the third season of drought."
    await store.add_memory(
        branch_id=branch,
        commit_id="c-old",
        kind="beat",
        text=old,
        vector=hashing_embedding(old),
        entity_refs=[],
    )
    engine = Engine(
        store,
        ProviderRouter(
            bindings={}, default=ScriptedProvider(completions=['{"actors":[],"claims":[]}'])
        ),
    )
    recall = await engine._recall(branch, "remind me about the oracle's flood prophecy")
    assert old in recall.memories


def _engine(store: PostgresEventStore, provider: ScriptedProvider) -> Engine:
    return Engine(store, ProviderRouter(bindings={}, default=provider))


async def test_recall_skips_zero_norm_query(store: PostgresEventStore) -> None:
    # A punctuation-only intent embeds to a zero vector; recall must return nothing
    # rather than let a NaN cosine inject arbitrary memories (review Phase-1.3).
    world = await store.create_world(f"test-{new_id()}")
    branch = world.main_branch_id
    m = "The oracle spoke of a coming flood."
    await store.add_memory(
        branch_id=branch,
        commit_id="c",
        kind="beat",
        text=m,
        vector=hashing_embedding(m),
        entity_refs=[],
    )
    engine = _engine(store, ScriptedProvider(completions=['{"actors":[],"claims":[]}']))
    recall = await engine._recall(branch, "???")  # no [a-z0-9] tokens → zero vector
    assert recall.memories == []


async def test_beat_survives_search_dimension_mismatch(store: PostgresEventStore) -> None:
    # A memory stored at 256-dim + an embedder producing 8-dim → the vector search errors;
    # the beat must degrade to structured-only recall, not crash (review Phase-1.3).
    world = await store.create_world(f"test-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    m = "The blacksmith forged a silver key."
    await store.add_memory(
        branch_id=campaign.branch_id,
        commit_id="c",
        kind="beat",
        text=m,
        vector=hashing_embedding(m),
        entity_refs=[],
    )
    engine = _engine(store, _WrongDimProvider(narration="A quiet night falls."))
    result = await engine.run_beat(campaign, "player-1", "I look around")  # must not raise
    assert result.commit_id


async def test_beat_commits_even_if_embedder_raises(store: PostgresEventStore) -> None:
    world = await store.create_world(f"test-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    engine = _engine(store, _RaisingEmbedProvider(narration="The cat stirs."))
    result = await engine.run_beat(campaign, "player-1", "I wait by the fire")
    assert result.commit_id  # beat committed despite embed failures in recall AND memory-write
    assert len(await store.recent_beats(campaign.branch_id, 5)) == 1


async def test_recall_deduplicates_identical_memories(store: PostgresEventStore) -> None:
    world = await store.create_world(f"test-{new_id()}")
    branch = world.main_branch_id
    m = "A distinctive omen concerning the red comet."
    for cid in ("c1", "c2"):
        await store.add_memory(
            branch_id=branch,
            commit_id=cid,
            kind="beat",
            text=m,
            vector=hashing_embedding(m),
            entity_refs=[],
        )
    engine = _engine(store, ScriptedProvider(completions=['{"actors":[],"claims":[]}']))
    recall = await engine._recall(branch, "tell me about the red comet omen")
    assert recall.memories.count(m) == 1  # two membership rows, one recall line
