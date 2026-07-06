"""Phase 1 increment 2: the extractor → gauntlet → recall loop (prose becomes canon).

Gauntlet tests inspect the events it produces; the engine test drives the full loop
with a scripted provider and shows extracted state resurfacing via recall.
"""

from collections.abc import AsyncIterator

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import DomainEvent, actor_created, claim_recorded
from uro_core.domain.ids import new_id
from uro_core.pipeline.engine import Engine
from uro_core.pipeline.extraction import (
    Extraction,
    ProposedActor,
    ProposedClaim,
    run_gauntlet,
)
from uro_core.pipeline.recall import assemble_recall
from uro_core.providers.adapters.stub import hashing_embedding
from uro_core.providers.base import CompletionRequest
from uro_core.providers.router import ProviderRouter


class ScriptedProvider:
    """Streams a fixed narration; returns queued JSON for each extractor call."""

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


async def _branch(store: PostgresEventStore) -> str:
    world = await store.create_world(f"test-{new_id()}")
    return world.main_branch_id


def _of_type(events: list[DomainEvent], t: str) -> list[DomainEvent]:
    return [e for e in events if e.event_type == t]


async def test_narrator_claim_becomes_true(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    ex = Extraction(
        claims=[ProposedClaim(statement="The cellar door is locked.", provenance="narrator")]
    )
    events = await run_gauntlet(store, branch, ex)
    claims = _of_type(events, "ClaimRecorded")
    assert len(claims) == 1 and claims[0].payload["truth"] == "true"
    assert _of_type(events, "BeliefChanged") == []  # narrator fact, no belief


async def test_dialogue_claim_is_testimony_plus_belief(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    await store.append_beat(branch, [actor_created(actor_id="a:flora", name="Flora", tier=2)])
    ex = Extraction(
        claims=[
            ProposedClaim(
                statement="The Duke plans war.",
                about=["Duke"],
                provenance="dialogue",
                speaker="Flora",
            )
        ]
    )
    events = await run_gauntlet(store, branch, ex)
    claim = _of_type(events, "ClaimRecorded")[0]
    assert claim.payload["truth"] == "unknown"  # a character saying it ≠ truth
    belief = _of_type(events, "BeliefChanged")[0]
    assert belief.payload["actor_id"] == "a:flora"  # resolved the speaker
    assert belief.payload["claim_id"] == claim.payload["claim_id"]


async def test_contradiction_downgrades_a_would_be_fact(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    await store.append_beat(
        branch,
        [
            claim_recorded(
                claim_id="c:pacifist",
                statement="The Duke disbanded his army.",
                subject_refs=["name:duke"],
                truth="true",
            )
        ],
    )
    # A new narrator-asserted claim that the extractor flags as contradicting the fact.
    ex = Extraction(
        claims=[
            ProposedClaim(
                statement="The Duke masses troops.",
                about=["Duke"],
                provenance="narrator",
                contradicts=["c:pacifist"],
            )
        ]
    )
    events = await run_gauntlet(store, branch, ex)
    claim = _of_type(events, "ClaimRecorded")[0]
    assert claim.payload["truth"] == "unknown"  # downgraded — can't hold two contradictory truths


async def test_dialogue_claim_about_speaker_links_to_actor_id(store: PostgresEventStore) -> None:
    # A speaker not pre-listed as an actor, asserting something about themselves: the
    # claim subject and the belief must resolve to the SAME minted actor_id, not diverge
    # into a name-token vs a:uuid (review Phase-1.2).
    branch = await _branch(store)
    ex = Extraction(
        claims=[
            ProposedClaim(
                statement="I poisoned the ale.",
                about=["Flora"],
                provenance="dialogue",
                speaker="Flora",
            )
        ]
    )
    events = await run_gauntlet(store, branch, ex)
    actor_ref = _of_type(events, "ActorCreated")[0].payload["actor_id"]
    claim = _of_type(events, "ClaimRecorded")[0]
    belief = _of_type(events, "BeliefChanged")[0]
    assert claim.payload["subject_refs"] == [actor_ref]  # linked to the actor, not name:flora
    assert belief.payload["actor_id"] == actor_ref


async def test_entity_resolution_deduplicates_actors(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    await store.append_beat(branch, [actor_created(actor_id="a:weck", name="Weck", tier=1)])
    ex = Extraction(actors=[ProposedActor(name="Weck"), ProposedActor(name="Bran")])
    events = await run_gauntlet(store, branch, ex)
    created = _of_type(events, "ActorCreated")
    assert [e.payload["name"] for e in created] == ["Bran"]  # Weck linked, only Bran created
    assert created[0].payload["tier"] == 1  # tier ceiling


async def test_engine_extracts_state_and_recall_resurfaces_it(store: PostgresEventStore) -> None:
    world = await store.create_world(f"test-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    branch = campaign.branch_id

    extraction_json = (
        '{"actors":[{"name":"Flora","role":"innkeeper"}],'
        '"claims":[{"statement":"The Duke disbanded his army.","about":["Duke"],'
        '"provenance":"narrator"}]}'
    )
    provider = ScriptedProvider(
        narration="Flora wipes a mug. 'The Duke? He disbanded his army years ago.'",
        completions=[extraction_json],
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=provider))

    result = await engine.run_beat(campaign, "player-1", "I ask Flora about the Duke")
    assert result.extracted == 2  # Flora (actor) + the Duke claim

    # State was committed and projected.
    assert (await store.find_actor_by_name(branch, "Flora")) is not None
    duke_claims = await store.claims_about(branch, "name:duke")
    assert len(duke_claims) == 1 and duke_claims[0].truth == "true"

    # A later beat mentioning the Duke recalls the established fact.
    recall = await assemble_recall(store, branch, "what do I know about the Duke?", 8)
    assert any(c.statement == "The Duke disbanded his army." for c in recall.claims)


async def test_fact_consistency_metric(store: PostgresEventStore) -> None:
    # Thesis metric T2: narrator claims surviving as truth=true are consistent; those
    # downgraded to unknown are not; dialogue (testimony) claims are excluded.
    branch = await _branch(store)
    await store.append_beat(
        branch,
        [
            claim_recorded(claim_id="c1", statement="A", truth="true", origin="narrator"),
            claim_recorded(claim_id="c2", statement="B", truth="true", origin="narrator"),
            claim_recorded(claim_id="c3", statement="C", truth="unknown", origin="narrator"),
            claim_recorded(claim_id="c4", statement="D", truth="unknown", origin="dialogue"),
        ],
    )
    consistent, total = await store.fact_consistency(branch)
    assert (consistent, total) == (2, 3)  # dialogue excluded; one narrator claim downgraded


async def test_bare_mode_is_a_true_ablation(store: PostgresEventStore) -> None:
    # The T1 baseline: same scripted narration + extraction, but bare mode records ONLY
    # the transcript — no state, no memory — so it can be A/B'd against the full engine.
    world = await store.create_world(f"test-{new_id()}")
    campaign = await store.create_campaign(world.world_id, world.main_branch_id)
    provider = ScriptedProvider(
        narration="Flora reveals a hidden passage.",
        completions=['{"actors":[{"name":"Flora"}],"claims":[{"statement":"a passage exists"}]}'],
    )
    engine = Engine(store, ProviderRouter(bindings={}, default=provider), bare=True)
    result = await engine.run_beat(campaign, "player-1", "I ask Flora")

    assert result.extracted == 0  # bare → no extraction, though the script would have made 2
    assert await store.list_actors(campaign.branch_id) == []  # no state built
    hits = await store.search(campaign.branch_id, hashing_embedding("hidden passage"), k=5)
    assert hits == []  # no memory indexed
