"""Phase 1 increment 1: epistemic projections built from events, in one transaction.

Deterministic (no LLM). Establishes the state substrate the Phase 1 acceptance test
needs: claims carry engine ground-truth, actors carry beliefs, and both are queryable —
so a later NPC can contradict a lie because state says it's false.
"""

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import (
    actor_created,
    actor_promoted,
    belief_changed,
    claim_recorded,
    claim_truth_changed,
)
from uro_core.domain.ids import new_id


async def _branch(store: PostgresEventStore) -> str:
    world = await store.create_world(f"test-{new_id()}")
    return world.main_branch_id


async def test_actor_projection_and_lookup(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    await store.append_beat(
        branch,
        [
            actor_created(
                actor_id="a:mera", name="Mera", tier=2, role="innkeeper", aliases=["the barkeep"]
            )
        ],
    )

    actor = await store.get_actor(branch, "a:mera")
    assert actor is not None and actor.name == "Mera" and actor.tier == 2
    assert (await store.find_actor_by_name(branch, "MERA")).actor_id == "a:mera"  # case-insensitive
    assert (await store.find_actor_by_name(branch, "the barkeep")).actor_id == "a:mera"  # alias
    assert [a.actor_id for a in await store.list_actors(branch)] == ["a:mera"]


async def test_actor_promotion_updates_tier(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    await store.append_beat(branch, [actor_created(actor_id="a:weck", name="Old Weck", tier=1)])
    await store.append_beat(
        branch, [actor_promoted(actor_id="a:weck", from_tier=1, to_tier=2, reason="pinned")]
    )
    actor = await store.get_actor(branch, "a:weck")
    assert actor is not None and actor.tier == 2


async def test_claims_carry_truth_and_are_findable_by_subject(store: PostgresEventStore) -> None:
    branch = await _branch(store)
    await store.append_beat(
        branch,
        [
            claim_recorded(
                claim_id="c:war",
                statement="The Duke plans war.",
                subject_refs=["a:duke"],
                truth="unknown",
                origin="narration",
            )
        ],
    )
    assert (await store.get_claim(branch, "c:war")).truth == "unknown"
    about = await store.claims_about(branch, "a:duke")
    assert [c.claim_id for c in about] == ["c:war"]

    await store.append_beat(
        branch, [claim_truth_changed(claim_id="c:war", truth="false", cause="investigated")]
    )
    assert (await store.get_claim(branch, "c:war")).truth == "false"


async def test_the_lie_scenario_at_state_level(store: PostgresEventStore) -> None:
    # The state substrate for the Phase 1 acceptance test: an NPC believes a claim
    # the engine knows to be false. A later NPC can contradict it *from state*.
    branch = await _branch(store)
    await store.append_beat(
        branch,
        [
            actor_created(actor_id="a:innkeeper", name="Flora", tier=2, role="innkeeper"),
            claim_recorded(
                claim_id="c:war",
                statement="The Duke plans war.",
                subject_refs=["a:duke"],
                truth="false",  # engine ground truth: it's a lie
                origin="dialogue",
            ),
            belief_changed(
                actor_id="a:innkeeper", claim_id="c:war", confidence=0.9, learned_from="a:spy"
            ),
        ],
    )

    claim = await store.get_claim(branch, "c:war")
    assert claim is not None and claim.truth == "false"  # the world knows it's false

    beliefs = await store.beliefs_of(branch, "a:innkeeper")
    assert len(beliefs) == 1
    assert beliefs[0].claim_id == "c:war" and beliefs[0].confidence == 0.9  # Flora believes the lie
    assert beliefs[0].learned_from == "a:spy"


async def test_projection_is_atomic_with_the_commit(store: PostgresEventStore) -> None:
    # All events in one beat commit project together (or not at all).
    branch = await _branch(store)
    await store.append_beat(
        branch,
        [
            actor_created(actor_id="a:x", name="X", tier=1),
            claim_recorded(
                claim_id="c:x", statement="X is here.", subject_refs=["a:x"], truth="true"
            ),
            belief_changed(actor_id="a:x", claim_id="c:x", confidence=1.0),
        ],
    )
    assert await store.get_actor(branch, "a:x") is not None
    assert await store.get_claim(branch, "c:x") is not None
    assert len(await store.beliefs_of(branch, "a:x")) == 1
