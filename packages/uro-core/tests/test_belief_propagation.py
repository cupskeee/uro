"""Phase 5 inc 5.3: off-screen belief/rumor propagation (docs/02, OQ-4). Deterministic — no LLM.

The war-story substrate: a feat with surviving witnesses fans a belief out along `knows` contact
edges (confidence decaying per hop, tier-agnostic), each acquired belief recording learned_from —
so a downstream tavern NPC ends up believing a garbled (low-confidence) version, traceable back to
the witnesses. With zero witnesses, nothing propagates.
"""

from uro_core.adapters.postgres.store import PostgresEventStore
from uro_core.domain.events import actor_created, claim_recorded, edge_added
from uro_core.domain.ids import new_id
from uro_core.engines.actor import propagate_belief


async def _witness_chain(store: PostgresEventStore) -> tuple[str, str]:
    """A world with witness → runner → barkeep along `knows`, plus a feat claim. All T1."""
    world = await store.create_world(f"rumor-{new_id()}")
    branch = world.main_branch_id
    await store.append_beat(
        branch,
        [
            actor_created(actor_id="a:witness", name="A surviving raider", tier=1),
            actor_created(actor_id="a:runner", name="A road-runner", tier=1),
            actor_created(actor_id="a:barkeep", name="The tavern keeper", tier=1),
            edge_added(src="a:witness", rel_type="knows", dst="a:runner"),
            edge_added(src="a:runner", rel_type="knows", dst="a:barkeep"),
            claim_recorded(
                claim_id="c:feat",
                statement="A lone wizard split the warband's champion in two.",
                subject_refs=["a:witness"],
                truth="true",
                origin="chronicle",
            ),
        ],
    )
    return branch


async def _trace(store: PostgresEventStore, branch: str, actor: str) -> list[str]:
    """Walk learned_from from `actor` back to the first-hand witness."""
    chain = [actor]
    while True:
        beliefs = await store.beliefs_of(branch, chain[-1])
        belief = next((b for b in beliefs if b.claim_id == "c:feat"), None)
        if belief is None or belief.learned_from is None:
            break
        chain.append(belief.learned_from)
    return chain


async def test_rumor_reaches_a_downstream_npc_with_a_traceable_chain(
    store: PostgresEventStore,
) -> None:
    branch = await _witness_chain(store)

    events = await propagate_belief(store, branch, claim_id="c:feat", witnesses=["a:witness"])
    await store.append_beat(branch, events)

    # the tavern keeper (T1, never present at the feat) now believes a version of it...
    barkeep = next(b for b in await store.beliefs_of(branch, "a:barkeep") if b.claim_id == "c:feat")
    witness = next(b for b in await store.beliefs_of(branch, "a:witness") if b.claim_id == "c:feat")
    # ...garbled: third-hand confidence is well below the eyewitness's
    assert 0.2 <= barkeep.confidence < witness.confidence
    # ...and the chain traces back to the witness
    assert await _trace(store, branch, "a:barkeep") == ["a:barkeep", "a:runner", "a:witness"]


async def test_no_witnesses_no_rumor(store: PostgresEventStore) -> None:
    branch = await _witness_chain(store)
    events = await propagate_belief(store, branch, claim_id="c:feat", witnesses=[])
    assert events == []
    await store.append_beat(branch, [claim_recorded(claim_id=f"c:{new_id()}", statement="filler")])
    # nobody downstream ever heard of the feat
    assert not any(b.claim_id == "c:feat" for b in await store.beliefs_of(branch, "a:barkeep"))
